"""The forza2dsf CLI (`uv run dsff forza2dsf ...`)."""

from __future__ import annotations

import argparse
from pathlib import Path

from dsfforza.convert.forza2dsf.car import car_dims, load_car
from dsfforza.convert.forza2dsf.conversion import ConvertOptions, convert, entry_sizes, parse_lods
from dsfforza.convert.forza2dsf.materials import DEFAULT_LODS, DEFAULT_SHADOW_LOD, MDXN_LIMIT, NONR_LIMIT
from dsfforza.formats.chnk import read_chunk, write_chunk
from dsfforza.formats.vehicle_mesh import payload
from dsfforza.formats.vehicles import find, parse_sslp


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archive", type=Path, help="archive with the host vehicle (e.g. the clone)")
    parser.add_argument("out", type=Path)
    parser.add_argument("--id", type=int, required=True, help="base spool ID of the host vehicle")
    parser.add_argument("--car", type=Path, required=True, help="extracted Forza car folder")
    parser.add_argument("--name", required=True, help="car media name, e.g. NIS_370Z_10")
    parser.add_argument(
        "--host-axle-mid",
        type=float,
        help="DSF z of the host's axle midpoint (default: from its wheel table; Desanne: (1.583 - 1.352) / 2)",
    )
    parser.add_argument("--lods", default=DEFAULT_LODS, help="DSF mask:Forza LOD pairs for the full entry")
    parser.add_argument("--shadow-lod", type=int, default=DEFAULT_SHADOW_LOD)
    parser.add_argument("--exterior-lod", type=int, default=1, help="Forza LOD for the clean exterior entry")
    parser.add_argument("--gamedb", type=Path, help="gamedb.slt, for wheel placement when the car has no MAXData.xml")
    parser.add_argument("--skip-full", action="store_true", help="leave the full entry (N) as it is")
    parser.add_argument("--skip-exterior", action="store_true", help="leave the clean exterior (N+2000) as it is")
    parser.add_argument("--no-hubs", action="store_true", help="don't move the hierarchy's wheel hubs")
    parser.add_argument("--no-exhaust", action="store_true", help="keep the host's exhaust markers")
    parser.add_argument(
        "--keep-host-materials",
        action="store_true",
        help="keep the host's own textures and materials in the full entry",
    )
    parser.add_argument(
        "--fill-empty-parts", action="store_true", help="give host parts that lose their geometry a degenerate triangle"
    )
    parser.add_argument(
        "--keep-host-geometry",
        action="store_true",
        help="keep the host's submodels and every part except the body (part 0) and wheel",
    )
    parser.add_argument("--no-wheel", action="store_true", help="leave the host's wheel part alone")
    parser.add_argument(
        "--ignore-size-limit",
        action="store_true",
        help="write entries larger than the largest vanilla ones (to test the limit)",
    )
    parser.add_argument(
        "--empty-other-parts",
        action="store_true",
        help="with --keep-host-geometry: keep the host submodels in the package but take "
        "them out of the other parts' LOD lists",
    )
    parser.add_argument(
        "--textures",
        choices=("replace", "clone"),
        help="texture lamps, badges and interior from the car's atlases (dsf_materials): "
        "replace = new DDS in the host's chassis TEXC, clone = new TEXC and materials",
    )
    parser.add_argument("--atlas-size", help="WxH of the chassis atlas (default: the host texture's)")
    parser.add_argument(
        "--swatch-trims",
        action="store_true",
        help="with --textures: colour swatch trims (black, frame...) from the atlas",
    )
    parser.add_argument(
        "--merge-submodels",
        action="store_true",
        help="one body submodel per material instead of per (section, material)",
    )
    parser.add_argument(
        "--cockpit",
        action="store_true",
        help="rebuild the interior (N+1000) from <car>_cockpit.carbin and move the steering "
        "hinge, gauges and head camera (src/dsfforza/convert/cockpit/); use with --keep-host-materials",
    )
    parser.add_argument(
        "--cockpit-shell-lod", type=int, default=5, help="main-carbin LOD for the outer body in the interior entry"
    )
    parser.add_argument(
        "--cockpit-decimate",
        type=float,
        default=0.0,
        help="vertex-cluster cell size (m) for the cockpit sections; 0 = full detail",
    )
    args = parser.parse_args(argv)
    if args.out.resolve() == args.archive.resolve():
        raise SystemExit("refusing to overwrite the input archive")
    opts = ConvertOptions(
        lods=parse_lods(args.lods),
        shadow_lod=args.shadow_lod,
        exterior_lod=args.exterior_lod,
        host_axle_mid=args.host_axle_mid,
        skip_full=args.skip_full,
        skip_exterior=args.skip_exterior,
        no_hubs=args.no_hubs,
        exhaust=not args.no_exhaust,
        keep_host_materials=args.keep_host_materials,
        fill_empty_parts=args.fill_empty_parts,
        keep_host_geometry=args.keep_host_geometry,
        no_wheel=args.no_wheel,
        empty_other_parts=args.empty_other_parts,
        textures=args.textures,
        atlas_size=tuple(int(x) for x in args.atlas_size.split("x")) if args.atlas_size else None,
        swatch_trims=args.swatch_trims,
        merge_submodels=args.merge_submodels,
        cockpit=args.cockpit,
        gamedbs=[args.gamedb] if args.gamedb else [],
    )
    if args.cockpit:
        from dsfforza.convert import cockpit

        opts.cockpit_opts = cockpit.CockpitOptions(shell_lod=args.cockpit_shell_lod, decimate=args.cockpit_decimate)

    car = load_car(args.car, args.name)
    dims = car_dims(car, args.gamedb)
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = find(root.entries, "SSIC")
        records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
        index_by_id = {spool_id: index for spool_id, index, _ in records}
        full = root.entries[index_by_id[args.id]]
        exterior = root.entries[index_by_id[args.id + 2000]]
        interior = root.entries[index_by_id[args.id + 1000]]
        convert(stream, full, exterior, car, dims, opts, interior=interior)

        # Check sizes against the largest vanilla entries; oversized entries crash the game.
        too_big = []
        for kind, vehc in ((0, full), (1, interior), (2, exterior)):
            sizes = entry_sizes(vehc)
            print(
                f"kind {kind}: MDXN {sizes['MDXN']:#x} (limit {MDXN_LIMIT[kind]:#x}), "
                f"NONR {sizes['NONR']:#x} (limit {NONR_LIMIT[kind]:#x})"
            )
            if sizes["MDXN"] > MDXN_LIMIT[kind] or sizes["NONR"] > NONR_LIMIT[kind]:
                too_big.append(kind)
        if too_big and not args.ignore_size_limit:
            raise SystemExit(f"entries {too_big} exceed the vanilla size limits; use lower --lods / --exterior-lod")
        with args.out.open("wb") as out:
            write_chunk(out, stream, root.entries, root.version)
    print(f"wrote {args.out}")
