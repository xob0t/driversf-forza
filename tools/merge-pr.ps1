<#
.SYNOPSIS
Squash-merges a pull request and cleans up after it, safely.

.DESCRIPTION
1. Finds the PR's branch and, if a local worktree has it checked out, runs that worktree's
   tools\worktree-setup.ps1 -Unlink and checks no junction is left (some delete tools
   follow junctions into the main checkout's folders).
2. gh pr merge --squash --delete-branch (refuses a PR that isn't mergeable).
3. Removes the worktree folder if it is still there, deletes the local branch, prunes.
4. Fast-forwards the main checkout's master, unless a build or install is running from
   the main checkout (its workers import the code as they start, so pulling under them
   mixes two versions); then it says so and leaves master where it is.

.EXAMPLE
.\tools\merge-pr.ps1 118
.\tools\merge-pr.ps1 118 -NoPull
#>
param(
    [Parameter(Mandatory, Position = 0)] [int]$Number,
    [switch]$NoPull,          # don't fast-forward the main checkout's master
    [switch]$KeepWorktree     # unlink and merge, but leave the worktree folder
)
$ErrorActionPreference = 'Stop'

$main = (git -C $PSScriptRoot rev-parse --path-format=absolute --git-common-dir).Trim() | Split-Path
function Say([string]$Text) { Write-Host "  $Text" }

$pr = gh pr view $Number --json headRefName,state,mergeable,title | ConvertFrom-Json
if ($pr.state -ne 'OPEN') { throw "PR #${Number} is $($pr.state)" }
Write-Host "#${Number}: $($pr.title)"
if ($pr.mergeable -eq 'CONFLICTING') { throw "PR #$Number has conflicts: rebase its branch $($pr.headRefName) first" }

# The branch's worktree, if any.
$worktree = $null
$current = $null
foreach ($line in (git -C $main worktree list --porcelain)) {
    if ($line -like 'worktree *') { $current = $line.Substring(9) }
    elseif ($line -eq "branch refs/heads/$($pr.headRefName)") { $worktree = $current }
}
if ($worktree) {
    $wt = [IO.Path]::GetFullPath($worktree)
    if ($wt -eq [IO.Path]::GetFullPath($main)) { throw "the main checkout has $($pr.headRefName) checked out" }
    Say "worktree $wt"
    $setup = Join-Path $wt 'tools\worktree-setup.ps1'
    if (Test-Path -LiteralPath $setup) { & $setup -Unlink | ForEach-Object { Say $_ } }
    $links = Get-ChildItem -LiteralPath $wt -Recurse -Depth 3 -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue
    if ($links) { throw "junctions left in ${wt}: $(($links | ForEach-Object FullName) -join ', ')" }
}

gh pr merge $Number --squash --delete-branch | Out-Null
if ($LASTEXITCODE) { throw "gh pr merge $Number failed" }
$merged = (gh pr view $Number --json state,mergeCommit | ConvertFrom-Json)
if ($merged.state -ne 'MERGED') { throw "PR #$Number is $($merged.state) after the merge" }
Say "merged as $($merged.mergeCommit.oid.Substring(0, 7))"

if ($worktree -and -not $KeepWorktree -and (Test-Path -LiteralPath $worktree)) {
    git -C $main worktree remove --force $worktree 2>$null
    if (Test-Path -LiteralPath $worktree) { [IO.Directory]::Delete($worktree, $true) }
    Say "removed the worktree"
}
git -C $main worktree prune
if (git -C $main branch --list $pr.headRefName) { git -C $main branch -D $pr.headRefName | Out-Null; Say "deleted the local branch" }

if ($NoPull) { return }
$venv = [regex]::Escape((Join-Path $main '.venv'))
$busy = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $venv -and $_.CommandLine -match 'dsff|port_batch|port_car' }
if ($busy) {
    Say "not pulling: a build is running from the main checkout (PIDs $(($busy | ForEach-Object ProcessId) -join ', ')); pull master when it ends"
    return
}
if ((git -C $main rev-parse --abbrev-ref HEAD).Trim() -ne 'master') { Say 'not pulling: the main checkout is not on master'; return }
git -C $main pull -q --ff-only
Say "master at $((git -C $main log --oneline -1).Trim())"
