"""`dsff <tool> [args]`: run one of the package's command-line tools.

    uv run dsff port_batch build --out out/batch/b1 --gui
    uv run dsff host_select rank NIS_370Z_10
    uv run dsff --help                  # the list of tools

`dsff <tool>` is the same as `python -m dsfforza.<group>.<tool>`; dashes work
for underscores (`dsff port-batch`).
"""

from __future__ import annotations

import importlib
import sys

from dsfforza import safe_stdio

# tool name -> module; every module here has a main().
TOOLS = {
    name: f"dsfforza.{group}.{name}"
    for group, names in {
        "formats": "carbin chnk forza_audio forza_strings forzazip fxobj lua_scripts minidump sevenzip stfs upvh vehicle_mesh "
        "vehicles wwise_bank xds xiso",
        "convert": "chassis_variants cockpit cockpit_survey damage driver_survey dsf_materials engine_sound exhaust "
        "forza2dsf forza_materials glass lights paint plates plates_survey steering_axle wheel_clearance",
        "build": "clone_vehicle forza_roster handling handling_dsf handling_tune host_select install package port_batch port_car report thirdparty",
        "game": "game_data gui_assets savecheck spool_variants traffic_sets vehicle_limits xref",
        "harness": "dyno emblem_check lights_diff render_compare vnckeys vncshot",
    }.items()
    for name in names.split()
}


def usage() -> str:
    lines = [__doc__.strip(), "", "tools:"]
    by_group: dict[str, list[str]] = {}
    for name, module in TOOLS.items():
        by_group.setdefault(module.split(".")[1], []).append(name)
    lines += [f"  {group + ':':9} {' '.join(sorted(names))}" for group, names in by_group.items()]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int | None:
    argv = sys.argv[1:] if argv is None else argv
    safe_stdio()
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(usage())
        return 0 if argv else 2
    name = argv[0].replace("-", "_")
    if name not in TOOLS:
        print(f"dsff: unknown tool {argv[0]!r}\n\n{usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(TOOLS[name])
    sys.argv = [f"dsff {name}", *argv[1:]]  # argparse's prog and the tool's own argv
    return module.main()


if __name__ == "__main__":
    sys.exit(main())
