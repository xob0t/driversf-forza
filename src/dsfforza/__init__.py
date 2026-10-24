"""Forza (FH1/FM4) to Driver: San Francisco car porting tools.

Subpackages:
    formats  file formats: CHNK archives, Forza carbin/xds/zip/STFS/XISO, Wwise/FSB audio, ...
    convert  Forza car -> DSF vehicle: meshes, materials, cockpit, damage, glass, plates, lights, sound
    build    the porting pipeline: host choice, cloning, handling, port_car and port_batch
    game     the game's own data: game folder, Lua scripts, GUI assets, limits, Driver.exe
    harness  in-game test analysis and VM helpers

Every tool with a CLI runs as `dsff <tool>` (dsfforza.cli) or `python -m dsfforza.<group>.<tool>`.
"""

from pathlib import Path

# The repo checkout (the package is installed editable from src/): out/, assets/,
# research/ and tools/ (the game Lua, the plugin, the test harness) live here.
REPO = Path(__file__).resolve().parents[2]
