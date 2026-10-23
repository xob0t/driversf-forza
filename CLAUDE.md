# Working in this repo

Ports Xbox 360 Forza (FH1/FM4) cars into Driver: San Francisco PC 1.04.
The repo ships code and manifests only, never game or Forza assets.
Research notes are in `research/` (read the ones for your area first).

## Agent workflow

- Work in your own git worktree on a branch named for the task
  (`fix/cockpit-glass`, `feat/engine-sound`, ...), never in the main checkout.
- In a new worktree run `.\tools\worktree-setup.ps1` once. It links the
  untracked inputs from the main checkout: `assets\*.iso`, `out\stock`,
  `out\cache`, `_ref`. Before the worktree is removed, run
  `.\tools\worktree-setup.ps1 -Unlink`: some delete tools follow junctions
  and would empty the main checkout's folders.
- Commit at checkpoints with clear messages. When the task is done, push the
  branch and open a PR against `master` with `gh pr create`: what changed,
  why, how it was verified (run folders, screenshots), what is open. Report
  the PR URL.
- Keep the PR to one topic. Don't reformat or touch unrelated files.

## Machine rules

- The user uses this PC. Builds: if `out\gentle` exists, `port_batch` runs at
  Idle priority on two cores; keep `--jobs` low then.
- In-game tests go through `tools\wsb\run-series.ps1` (read it and
  `tools\wsb\guest\run-test.ps1`). Runs are serialised by a mutex; never kill
  `Driver.exe` or another run's processes. Host runs (`-OnHost`) take over the
  screen; use them only when the user allows. While `out\wsb-pause` exists,
  host runs wait.
- Never modify the installed game, `D:\Games\Driver San Francisco`, except
  through `tools\install.ps1` when asked.
