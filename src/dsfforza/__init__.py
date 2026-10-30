"""Forza (FH1/FM4) to Driver: San Francisco car porting tools.

Subpackages:
    formats  file formats: CHNK archives, Forza carbin/xds/zip/STFS/XISO, Wwise/FSB audio, ...
    convert  Forza car -> DSF vehicle: meshes, materials, cockpit, damage, glass, plates, lights, sound
    build    the porting pipeline: host choice, cloning, handling, port_car and port_batch
    game     the game's own data: game folder, Lua scripts, GUI assets, limits, Driver.exe
    harness  in-game test analysis and VM helpers

Every tool with a CLI runs as `dsff <tool>` (dsfforza.cli) or `python -m dsfforza.<group>.<tool>`.
"""

import sys
from pathlib import Path

# The repo checkout (the package is installed editable from src/): out/, assets/,
# research/ and tools/ (the game Lua, the plugin, the test harness) live here.
REPO = Path(__file__).resolve().parents[2]


def safe_stdio() -> None:
    """Make print() survive any text: stdout and stderr redirected to a file or pipe
    write UTF-8 (Python would use the console's code page, cp437/cp1252, and raise on
    a Forza name with a letter outside it); on a console, characters it
    can't show become '?'."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream.isatty():
                stream.reconfigure(errors="replace")
            else:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):  # replaced by a non-TextIOWrapper, or closed
            pass
