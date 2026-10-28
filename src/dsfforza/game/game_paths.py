"""Vanilla game files when the game folder has a dsfforza install.

tools/install.ps1 replaces Install/dngvehicles.sp, Install/gui.sp,
media/Fonts.fchunk and Locale/<language>/global.fchunk and keeps the originals
in <game>/dsfforza-backup/<same path>. Builds must read the originals, so every
tool that takes a game file goes through vanilla() instead of opening it
directly.

The repo also keeps its own copy of those originals in out/vanilla/<same path>
(made once, while the game folder was vanilla). That copy is preferred: the
sandbox harness links it into test runs, so a running test never holds files
inside the user's game folder open (which blocked install.ps1 once).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dsfforza import REPO

BACKUP = "dsfforza-backup"
# Resolved: in a git worktree out/vanilla is a junction to the main checkout's
# (tools/worktree-setup.ps1), and the paths compared with it are resolved too.
_legacy = REPO / "out" / "stock"  # the folder's name before v0.1.3
if (
    not (REPO / "out" / "vanilla").exists()
    and _legacy.is_dir()
    and not _legacy.is_symlink()
    and not _legacy.is_junction()
):
    _legacy.rename(REPO / "out" / "vanilla")
REPO_VANILLA = (REPO / "out" / "vanilla").resolve()


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
    """The game's Install/dngvehicles.sp (see vanilla() for the original after an install)."""
    return game_dir() / "Install" / "dngvehicles.sp"


def game_dir_of(path: Path) -> Path | None:
    """The game folder of a file inside it (the folder holding Install/), or None."""
    path = Path(path).resolve()
    for parent in path.parents:
        if (parent / "Install").is_dir() and (parent / "media").is_dir():
            return parent
    return None


def vanilla(path: Path | None, quiet: bool = False) -> Path:
    """`path` (default: the game's dngvehicles.sp), or its original when a dsfforza
    install replaced it: the repo's out/vanilla copy if it has one, else the install's backup."""
    path = Path(path) if path is not None else game_archive()
    game = game_dir_of(path)
    if game is None or game.name == BACKUP or game == REPO_VANILLA:
        return path
    if not (game / BACKUP).is_dir():
        return path  # no dsfforza install: the game's own file is vanilla
    relative = path.resolve().relative_to(game)
    for original in (REPO_VANILLA / relative, game / BACKUP / relative):
        if original.is_file():
            if not quiet:
                print(
                    f"note: {path} is replaced by a dsfforza install; reading the original {original}", file=sys.stderr
                )
            return original
    return path
