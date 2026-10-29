<#
.SYNOPSIS
Links a git worktree to the main checkout's untracked inputs: the Forza disc
images (assets\), the vanilla game copy (out\vanilla), the extraction cache
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

Worktrees removed without -Unlink (the agent host deletes a finished worker's
checkout but leaves its folder and links, issue #265) are cleaned up from the
main checkout with

    .\tools\worktree-setup.ps1 -Sweep [-DryRun]

In every folder under the worktree roots that `git worktree list` no longer has
(or marks prunable), it removes the junctions and directory symlinks that point
into the main checkout, the link only, never its target, and prints each one.
The roots are the parent folders of the listed worktrees (except the main
checkout's own parent, where only <main checkout name>-* folders count), the
main checkout's worktrees\ and %USERPROFILE%\.t3\worktrees\<main checkout name>.
tools\merge-pr.ps1 runs it after each merge. -DryRun only lists the links.
#>
param([switch]$Unlink, [switch]$Sweep, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
$here = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
# The main checkout is the first entry of `git worktree list`.
$main = ((git -C $here worktree list --porcelain) | Where-Object { $_ -like 'worktree *' } | Select-Object -First 1).Substring(9) -replace '/', '\'

function Get-FullPath([string]$Path) { [IO.Path]::GetFullPath(($Path -replace '/', '\')).TrimEnd('\') }

function Get-LinkTarget([string]$Path) {
    # A junction's or directory symlink's target as a full path, or $null.
    $target = @((Get-Item -LiteralPath $Path -Force).Target) | Select-Object -First 1
    if (-not $target) { return $null }
    $target = $target -replace '^\\(\?\?|\\\?)\\', ''
    if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path (Split-Path $Path) $target }
    Get-FullPath $target
}

function Find-Links([IO.DirectoryInfo]$Dir, [int]$Depth) {
    # Directory reparse points $Depth levels down from $Dir, without entering any of them
    # or a live worktree ($live, set by -Sweep) nested in it.
    try { $children = @($Dir.GetDirectories()) } catch { return }
    foreach ($child in $children) {
        if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) { $child.FullName }
        elseif ($Depth -gt 1 -and -not $live.Contains((Get-FullPath $child.FullName))) { Find-Links $child ($Depth - 1) }
    }
}

if ($Sweep) {
    $mainFull = Get-FullPath $main
    $mainParent = Split-Path $mainFull
    $mainName = Split-Path $mainFull -Leaf
    # Live worktrees: listed and not prunable (a prunable one has lost its checkout or its .git file).
    $live = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $current = $null
    foreach ($line in @(git -C $here worktree list --porcelain) + '') {
        if ($line -like 'worktree *') { $current = Get-FullPath $line.Substring(9) }
        elseif ($line -like 'prunable*') { $current = $null }
        elseif ($line -eq '' -and $current) { [void]$live.Add($current); $current = $null }
    }
    [void]$live.Add($mainFull)
    $roots = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    foreach ($wt in $live) {
        $parent = Split-Path $wt
        if ($wt -ne $mainFull -and $parent -ne $mainParent) { [void]$roots.Add($parent) }
    }
    foreach ($root in (Join-Path $mainFull 'worktrees'), (Join-Path $env:USERPROFILE ".t3\worktrees\$mainName")) {
        if (Test-Path -LiteralPath $root -PathType Container) { [void]$roots.Add((Get-FullPath $root)) }
    }
    $folders = @(foreach ($root in $roots) { Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue })
    $folders += @(Get-ChildItem -LiteralPath $mainParent -Directory -Force -Filter "$mainName-*" -ErrorAction SilentlyContinue)
    # Not a live worktree, not itself a link, not a clone of its own (a .git folder, not a worktree's .git file).
    $leftover = @($folders | Sort-Object FullName -Unique | Where-Object {
        -not $live.Contains((Get-FullPath $_.FullName)) -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -and
        -not (Test-Path -LiteralPath (Join-Path $_.FullName '.git') -PathType Container) })
    $verb = if ($DryRun) { 'would unlink' } else { 'unlinked' }
    $count = 0
    foreach ($dir in $leftover) {
        # The setup's links are at most two levels down (out\cache); three levels stop short of a build's car folders.
        foreach ($link in (Find-Links $dir 3)) {
            $target = Get-LinkTarget $link
            if (-not $target -or -not ($target -eq $mainFull -or $target.StartsWith("$mainFull\", [StringComparison]::OrdinalIgnoreCase))) {
                "kept $link (points to $target, outside the main checkout)"
                continue
            }
            if (-not $DryRun) { [IO.Directory]::Delete($link) }   # not recursive: removes the link only
            "$verb $link -> $target"
            $count++
        }
    }
    "sweep: $verb $count link(s) in $($leftover.Count) folder(s) that are no longer worktrees ($(@($roots) -join ', '), $mainParent\$mainName-*)"
    return
}
if ((Resolve-Path $main).Path -eq $here) { "this is the main checkout; nothing to link"; return }
$links = 'assets', 'out\vanilla', 'out\cache', 'out\tools-dl', '_ref'
# out\stock: the vanilla copy's link in worktrees made before v0.1.3 (it was called stock).
if ($Unlink) { $links += 'out\stock' }
foreach ($rel in $links) {
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
# uv reports progress on stderr; under 'Stop', Windows PowerShell turns the
# first stderr line of a native command into a terminating error.
$ErrorActionPreference = 'Continue'
& $uv.Source run --project $here dsff game_data prepare
if ($LASTEXITCODE -ne 0) { Write-Warning "game_data prepare failed (exit $LASTEXITCODE); see above" }
