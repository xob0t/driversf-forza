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
- Commit at checkpoints with clear messages. When the task is done, push the
  branch and open a PR against `master` with `gh pr create`: what changed,
  why, how it was verified (run folders, screenshots), what is open. Report
  the PR URL.
- Visual changes (anything that changes how a car looks in game or in a
  render): put before/after images in the PR when possible, same view and
  framing side by side, cropped to what changed. Commit them under
  `research/img/` (PNG or JPG, under ~500 KB each) and embed them in the PR
  description by commit SHA, not branch name (branches are deleted on merge):
  `https://github.com/xob0t/driversf-forza/blob/<commit-sha>/research/img/<file>?raw=true`
  with the SHA of a commit on your branch that contains the image.
- Keep the PR to one topic. Don't reformat or touch unrelated files.
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
  guest, never touches the user's screen; its own mutex). Host runs
  (`-OnHost`) take over the screen; use them only when the user allows.
  While `out\wsb-pause` exists, host runs wait.
- Never modify the installed game (`DSF_GAME_DIR`) except through
  `tools\install.ps1` when asked.
