"""Checks for the command-line entry points (src/dsfforza/cli.py, port_batch.main) that need
no game files.

uv run python tests/test_cli.py     # or: uv run python -m tests cli
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from dsfforza import REPO

NAME = "CIT_Élysée_Győr_08"  # É is in cp1252, ő is not

# Runs port_batch's plan printing (the build prints its GUI step's lines the same way)
# through an entry point, with cmd_plan and the game folder lookup stubbed out.
SCRIPT = f"""
import sys
from dsfforza import cli
from dsfforza.build import port_batch
from dsfforza.game import game_paths

game_paths.vanilla = lambda path: path
port_batch.cmd_plan = lambda args: port_batch.print_plan([port_batch.Pick({NAME!a}, "FM4", 2008, "FM4", {{}})])
if sys.argv[1] == "cli":
    sys.exit(cli.main(["port_batch", "plan", "--dsf", "x.sp"]))
port_batch.main(["plan", "--dsf", "x.sp"])
"""


def _run(entry: str) -> tuple[int, bytes, str]:
    """(exit code, stdout bytes, stderr) of SCRIPT with stdout redirected to a file on a cp1252 console."""
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}  # what a redirected stream gets on a cp1252 Windows
    env.pop("PYTHONUTF8", None)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "build.log"
        with out.open("wb") as f:
            result = subprocess.run(
                [sys.executable, "-c", SCRIPT, entry], stdout=f, stderr=subprocess.PIPE, env=env, cwd=REPO, check=False
            )
        return result.returncode, out.read_bytes(), result.stderr.decode("utf-8", "replace")


def test_redirected_output_on_a_cp1252_console_takes_any_name():
    """Issue #128: a Forza name outside the console's code page raised UnicodeEncodeError
    in port_batch build --gui > build.log; redirected output is now UTF-8."""
    for entry in ("cli", "port_batch"):  # dsff port_batch ..., python -m dsfforza.build.port_batch ...
        code, out, err = _run(entry)
        assert code == 0, (entry, err)
        assert NAME.encode("utf-8") in out, (entry, out)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
