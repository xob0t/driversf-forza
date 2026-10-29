"""tools/worktree-setup.ps1 -Sweep: in folders under the worktree roots that are no longer
git worktrees, it removes the junctions into the main checkout and only them (issue #265).
Runs on a throwaway repo in a temp folder, never the real checkout. Offline, a few seconds.

uv run python -m tests worktree_sweep
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from dsfforza import REPO

SCRIPT = REPO / "tools" / "worktree-setup.ps1"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True,
                   capture_output=True)  # fmt: skip


def _junction(link: Path, target: Path) -> Path:
    link.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    return link


def _file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("data", encoding="utf-8")
    return path


def _sweep(main: Path, home: Path, *extra: str) -> str:
    env = dict(os.environ, USERPROFILE=str(home))  # the T3 root, %USERPROFILE%\.t3\worktrees\<name>
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         str(main / "tools" / "worktree-setup.ps1"), "-Sweep", *extra],
        check=False, capture_output=True, text=True, timeout=120, env=env,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _remove_links(folder: Path) -> None:
    # Junctions first (rmdir removes the link only), so the cleanup can't follow one.
    for child in folder.iterdir():
        if child.is_junction():
            child.rmdir()
        elif child.is_dir() and not child.is_symlink():
            _remove_links(child)


def _writable(func, path, _exc) -> None:
    # git's object files are read-only, which stops rmtree on Windows.
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def test_sweep_removes_only_the_links_of_folders_that_are_no_longer_worktrees():
    tmp = Path(tempfile.mkdtemp(prefix="test-sweep-")).resolve()
    try:
        main = tmp / "main"
        data = [_file(main / "assets" / "disc.iso"), _file(main / "out" / "cache" / "c.txt"),
                _file(main / "_ref" / "r.txt"), _file(tmp / "outside" / "o.txt")]  # fmt: skip
        (main / "tools").mkdir()
        shutil.copy(SCRIPT, main / "tools")
        (main / ".gitignore").write_text("assets/\nout/\n_ref/\nworktrees/\n", encoding="utf-8")
        _git(main, "init", "-q", "-b", "master")
        _git(main, "add", "-A")
        _git(main, "commit", "-q", "-m", "init")
        _git(main, "worktree", "add", "-q", str(main / "worktrees" / "live"), "-b", "live")
        _git(main, "worktree", "add", "-q", str(main / "worktrees" / "broken"), "-b", "broken")
        _git(main, "worktree", "add", "-q", str(tmp / "wt" / "alive"), "-b", "alive")  # makes wt\ a root
        (main / "worktrees" / "broken" / ".git").unlink()  # still listed, but prunable
        home = tmp / "home"

        kept = [
            _junction(main / "worktrees" / "live" / "assets", main / "assets"),  # a live worktree
            _junction(tmp / "other" / "assets", main / "assets"),  # not a worktree root, not main-*
            _junction(main / "worktrees" / "gone" / "out" / "elsewhere", tmp / "outside"),  # not into main
            _junction(tmp / "main-clone" / "assets", main / "assets"),  # a clone of its own, not a worktree
        ]
        (tmp / "main-clone" / ".git").mkdir()
        removed = [
            _junction(main / "worktrees" / "gone" / "assets", main / "assets"),
            _junction(main / "worktrees" / "gone" / "out" / "cache", main / "out" / "cache"),
            _junction(main / "worktrees" / "broken" / "_ref", main / "_ref"),
            _junction(tmp / "main-old" / "out" / "stock", main / "out" / "cache"),  # a sibling main-*
            _junction(tmp / "wt" / "dead" / "out" / "tools-dl", main / "out" / "cache"),
            _junction(home / ".t3" / "worktrees" / "main" / "t3-x" / "assets", main / "assets"),
        ]

        out = _sweep(main, home, "-DryRun")
        assert sum(line.startswith("would unlink ") for line in out.splitlines()) == len(removed), out
        assert all(link.exists() for link in kept + removed), out

        out = _sweep(main, home)
        for link in removed:
            assert not os.path.lexists(link), out
            assert f"unlinked {link} -> " in out, out
        assert all(os.path.isjunction(link) for link in kept), out
        assert f"kept {kept[2]}" in out, out
        assert all(path.read_text(encoding="utf-8") == "data" for path in data), out
        assert "sweep: unlinked 6 link(s)" in out, out
        assert "sweep: unlinked 0 link(s)" in _sweep(main, home)
    finally:
        _remove_links(tmp)
        shutil.rmtree(tmp, onexc=_writable)
