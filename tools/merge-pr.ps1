<#
.SYNOPSIS
Squash-merges a pull request and cleans up after it, safely.

.DESCRIPTION
1. Points open PRs stacked on this one at master (deleting their base would close them).
2. gh pr merge --squash (refuses a PR that isn't mergeable). Not --delete-branch: with it,
   gh also removes the local worktree that has the branch checked out, whatever is in it.
   GitHub deletes the remote branch (the repo's delete_branch_on_merge).
3. Leaves the PR's worktree alone: its agent may still be working in it (merges deleted a
   worker's folder twice, once when the worker had committed and pushed everything). The
   agent removes it when done (worktree-setup.ps1 -Unlink first). With -RemoveWorktree, a
   clean worktree is unlinked (worktree-setup.ps1 -Unlink, checked for junctions left) and
   removed; one with uncommitted or untracked files is still left. Deletes the local branch
   unless a worktree has it checked out, prunes, and sweeps the links out of folders that
   are no longer worktrees (worktree-setup.ps1 -Sweep).
4. Fast-forwards the main checkout's master, unless a build or install is running from
   the main checkout (its workers import the code as they start, so pulling under them
   mixes two versions); then it says so and leaves master where it is.

.EXAMPLE
.\tools\merge-pr.ps1 118
.\tools\merge-pr.ps1 118 -NoPull
.\tools\merge-pr.ps1 118 -RemoveWorktree   # your own worktree, when nobody works in it
#>
param(
    [Parameter(Mandatory, Position = 0)] [int]$Number,
    [switch]$NoPull,          # don't fast-forward the main checkout's master
    [switch]$RemoveWorktree,  # unlink and remove the PR's worktree if it is clean
    [switch]$KeepWorktree     # the default now; kept so old calls still work
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
$keepNote = $null
if ($worktree -and -not $RemoveWorktree) { $keepNote = "left the worktree $worktree (its agent removes it; -RemoveWorktree to remove it now)" }
elseif ($worktree -and (git -C $worktree status --porcelain)) { $keepNote = "left the worktree ${worktree}: it has uncommitted or untracked files" }
if ($keepNote) { $worktree = $null }
if ($worktree) {
    $wt = [IO.Path]::GetFullPath($worktree)
    if ($wt -eq [IO.Path]::GetFullPath($main)) { throw "the main checkout has $($pr.headRefName) checked out" }
    Say "worktree $wt"
    $setup = Join-Path $wt 'tools\worktree-setup.ps1'
    if (Test-Path -LiteralPath $setup) { & $setup -Unlink | ForEach-Object { Say $_ } }
    $links = Get-ChildItem -LiteralPath $wt -Recurse -Depth 3 -Force -Attributes ReparsePoint -ErrorAction SilentlyContinue
    if ($links) { throw "junctions left in ${wt}: $(($links | ForEach-Object FullName) -join ', ')" }
}

# PRs stacked on this one: point them at master first. Deleting their base branch
# closes them, and a closed PR's base can't be changed.
$stacked = gh pr list --state open --base $pr.headRefName --json number --jq '.[].number'
foreach ($n in $stacked) {
    gh pr edit $n --base master | Out-Null
    if ($LASTEXITCODE) { throw "could not retarget stacked PR #$n to master" }
    Say "retargeted stacked PR #$n to master (rebase it onto master after this merge)"
}

# Not --delete-branch: with it, gh also removes the local worktree that has the branch
# checked out, whatever is in it. GitHub deletes the remote branch itself (the repo's
# delete_branch_on_merge is on), and the worktree is handled below.
gh pr merge $Number --squash | Out-Null
if ($LASTEXITCODE) { throw "gh pr merge $Number failed" }
$merged = (gh pr view $Number --json state,mergeCommit | ConvertFrom-Json)
if ($merged.state -ne 'MERGED') { throw "PR #$Number is $($merged.state) after the merge" }
Say "merged as $($merged.mergeCommit.oid.Substring(0, 7))"

if ($keepNote) { Say $keepNote }
if ($worktree -and (Test-Path -LiteralPath $worktree)) {
    # git's stderr must not reach PowerShell 5.1 as an error record under 'Stop'.
    $out = & cmd /c "git -C `"$main`" worktree remove --force `"$worktree`" 2>&1"
    if ($LASTEXITCODE -or (Test-Path -LiteralPath $worktree)) { Say "could not remove the worktree ($out); left it: remove it when nothing uses it" }
    else { Say "removed the worktree" }
}
git -C $main worktree prune
$checkedOut = (git -C $main worktree list --porcelain) -contains "branch refs/heads/$($pr.headRefName)"
if ($checkedOut) { Say "kept the local branch: a kept worktree has it checked out" }
elseif (git -C $main branch --list $pr.headRefName) { git -C $main branch -D $pr.headRefName | Out-Null; Say "deleted the local branch" }
try { & (Join-Path $PSScriptRoot 'worktree-setup.ps1') -Sweep | ForEach-Object { Say $_ } } catch { Say "sweep failed: $($_.Exception.Message)" }

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
