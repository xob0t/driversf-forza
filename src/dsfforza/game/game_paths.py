"""Stock game files when the game folder has a dsfforza install.

tools/install.ps1 replaces Install/dngvehicles.sp, Install/gui.sp,
media/Fonts.fchunk and Locale/<language>/global.fchunk and keeps the originals
in <game>/dsfforza-backup/<same path>. Builds must read the originals, so every
tool that takes a game file goes through stock() instead of opening it
directly.

The repo also keeps its own copy of those originals in out/stock/<same path>
(made once, while the game folder was stock). That copy is preferred: the
sandbox harness links it into test runs, so a running test never holds files
inside the user's game folder open (which blocked install.ps1 once).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dsfforza import REPO

BACKUP = "dsfforza-backup"
# Resolved: in a git worktree out/stock is a junction to the main checkout's
# (tools/worktree-setup.ps1), and the paths compared with it are resolved too.
REPO_STOCK = (REPO / "out" / "stock").resolve()


def game_dir() -> Path:
    """The game folder (the one with Driver.exe), from $DSF_GAME_DIR."""
    value = os.environ.get("DSF_GAME_DIR")
    if not value:
        raise SystemExit(
            "Set DSF_GAME_DIR to your Driver: San Francisco folder (the one with "
            "Driver.exe), or pass the game file on the command line."
        )
    return Path(value)


def game_archive() -> Path:
    """The game's Install/dngvehicles.sp (see stock() for the original after an install)."""
    return game_dir() / "Install" / "dngvehicles.sp"


def game_dir_of(path: Path) -> Path | None:
    """The game folder of a file inside it (the folder holding Install/), or None."""
    path = Path(path).resolve()
    for parent in path.parents:
        if (parent / "Install").is_dir() and (parent / "media").is_dir():
            return parent
    return None


def stock(path: Path | None, quiet: bool = False) -> Path:
    """`path` (default: the game's dngvehicles.sp), or its original when a dsfforza
    install replaced it: the repo's out/stock copy if it has one, else the install's backup."""
    path = Path(path) if path is not None else game_archive()
    game = game_dir_of(path)
    if game is None or game.name == BACKUP or game == REPO_STOCK:
        return path
    if not (game / BACKUP).is_dir():
        return path  # no dsfforza install: the game's own file is stock
    relative = path.resolve().relative_to(game)
    for original in (REPO_STOCK / relative, game / BACKUP / relative):
        if original.is_file():
            if not quiet:
                print(
                    f"note: {path} is replaced by a dsfforza install; reading the original {original}", file=sys.stderr
                )
            return original
    return path
