# Working in this repo

Instructions for coding agents (and people) working on this repo.

Ports Xbox 360 Forza (FH1/FM4) cars into Driver: San Francisco PC 1.04.
The repo ships code and manifests only, never game or Forza assets.
Research notes are in `research/` (read the ones for your area first).

## Agent workflow

- Work in your own git worktree on a branch named for the task
  (`fix/cockpit-glass`, `feat/engine-sound`, ...), never in the main checkout.
- In a new worktree run `.\tools\worktree-setup.ps1` once. It links the
  untracked inputs from the main checkout: `assets\*.iso`, `out\stock`,
  `out\cache`, `out\tools-dl`, `_ref`, and writes the worktree's own
  `out\lua-src` and `out\db` (`uv run dsff game_data prepare`). Before the
  worktree is removed, run
  `.\tools\worktree-setup.ps1 -Unlink`: some delete tools follow junctions
  and would empty the main checkout's folders.
- Never write to the main checkout or its `out\` (builds, installs and
  releases run there); write only under your worktree. Read-only use of an
  existing build there (e.g. `out\build` as a "before") is fine.
- Commit at checkpoints with clear messages. When the task is done, rebase
  onto `origin/master` (other agents merge while you work), rerun the tests,
  push the branch and open a PR against `master` with `gh pr create`: what
  changed, why, how it was verified (run folders, screenshots), what is open.
  CI runs the offline tests on the PR (`.github/workflows/tests.yml`).
- Your final report: the PR URL; the cause; what changed and on how many
  cars (from a roster survey, before and after); how it was verified (run
  folders); costs and what is left open; the issues you filed or commented
  on; the files you touched outside your task's area.
- No AI attribution in commits or PRs: no `Co-authored-by:` trailer for an
  assistant, no "Generated with ..." lines. PRs are squash-merged and the
  trailers end up on `master`.
- Visual changes (anything that changes how a car looks in game or in a
  render): put before/after images in the PR when possible, same view and
  framing side by side, cropped to what changed. Commit them under
  `research/img/` (PNG or JPG, under ~500 KB each) and embed them in the PR
  description by commit SHA, not branch name (branches are deleted on merge):
  `https://github.com/xob0t/driversf-forza/blob/<commit-sha>/research/img/<file>?raw=true`
  with the SHA of a commit on your branch that contains the image.
- Keep the PR to one topic. Don't reformat or touch unrelated files.
- Bugs outside your task, and the open items your PR leaves, go to GitHub
  issues for later agents rather than into your PR. First search the open and
  closed issues (`gh issue list --state all --search "<words>"`). If the bug is
  already there, add what you found as a comment (`gh issue comment`).
  Otherwise open one with `gh issue create --label bug` (`enhancement` for a
  missing feature). The issue says what is wrong and on which cars (spool ids
  or Forza names), how to reproduce it, and the evidence: the run folder,
  log lines, numbers, and screenshots committed under `research/img/` and
  linked by commit SHA (as for PRs). Also say what you ruled out and where you
  would start. Mention the issue numbers in your PR and in your report.
  Only file what differs from the stock game or from Forza. Behaviour the
  stock game has too (its layouts, its UI quirks) is not an issue; check a
  stock car or the unmodified game before filing.
- A comment, review or reply on an issue or PR someone else opened (a
  player's report, a contributor's PR) starts with the line
  `<model name> on behalf of xob0t:` (e.g. `Claude Opus 5.5 on behalf of
  xob0t:`), then a blank line and the response.
- Python: the package `dsfforza` in `src/dsfforza/` (`formats/`, `convert/`,
  `build/`, `game/`, `harness/`), managed with uv on Python 3.13
  (`pyproject.toml`, `uv.lock`). Run a tool with `uv run dsff <tool> ...`
  (`uv run dsff --help` lists them) or `uv run python -m
  dsfforza.<group>.<tool>`; a new CLI module also goes in `dsfforza/cli.py`'s
  `TOOLS`. Import repo modules as `from dsfforza.<group> import <module>`,
  repo paths from `dsfforza.REPO`. Dependencies: `uv add <pkg>` (`--group
  dev` for tools only developers need).
- Lint and format: ruff (config in `pyproject.toml`). Run `uv run ruff check
  --fix` and `uv run ruff format` before committing.
- Tests: `uv run python -m tests` runs `ruff check`, `ruff format --check` and
  every Python test file (`tests/test_*.py`; `--no-lint` skips ruff). New
  tests go in `tests/test_<area>.py`.

## Machine rules

- Tools read the game folder from `DSF_GAME_DIR` (set for the user on this PC);
  never hard-code a machine path in code or docs.
- The user uses this PC. Builds: if `out\gentle` exists, `port_batch` runs at
  Idle priority on two cores; keep `--jobs` low then.
- In-game tests go through `tools\wsb\run-series.ps1` (read it and
  `tools\wsb\guest\run-test.ps1`). Runs are serialised by a mutex; never kill
  `Driver.exe` or another run's processes. Prefer `-Vm` (headless VMware
  guests, never touch the user's screen; a series takes whichever of the two
  VMs is free, each with its own mutex). Host runs
  (`-OnHost`) take over the screen; use them only when the user allows.
  While `out\wsb-pause` exists, host runs wait.
- The two VMs are shared by every agent, so series queue (first come, first
  served). Make each run count:
  - put several cars in one series (`-ProbeIds '1,2,3'`) and the before and
    after archives in one call (`-Archives before.sp,after.sp`);
  - with more than two or three probe IDs or extra `-Views`, use
    `-ShotSeconds 3` and a longer `-WaitSeconds` (300 to 600), or the probe
    ends before it reaches the last cars;
  - a run that fails before the world loads (no desktop session, a hang on
    the way to Story mode, a slow load on a busy host) is usually the VM,
    not your change: rerun it once, on the other VM if you can, before
    looking for a cause in your build.
- Full-roster builds take over an hour and slow everyone's VM runs. Survey
  offline where you can, build only the cars your change affects for the
  in-game check, and keep `--jobs` at 4 or less while other agents run.
- Never modify the installed game (`DSF_GAME_DIR`) except through
  `tools\install.ps1` when asked.

## Coordinating agents

For the session that hands tasks to worker agents (`.claude/agents/dsff-worker.md`).

- At most four workers at once. More collide on the two VMs and the CPU, and
  a usage limit stops them all together.
- Hand out work by code area, not by symptom: one worker owns a module
  (`convert/lights.py`, `convert/cockpit.py`, `tools/asi/src/mod_*.cpp`, ...)
  until its PR merges. A task that needs a module another worker owns waits,
  or goes to that worker. Name the areas other workers own in each brief.
- The brief holds the task only (goal, cars, evidence, what's ruled out,
  where to start). The standing rules are this file and the agent definition.
- Merge with `.\tools\merge-pr.ps1 <number>` (unlinks the worktree, squash-
  merges, cleans up, and pulls master unless a build runs from the main
  checkout). After a merge, tell the workers whose areas it touched to rebase.
- Releases: freeze on a release branch first (`docs/DEVELOPMENT.md`,
  "Releases"); master keeps taking PRs while the release is verified.
