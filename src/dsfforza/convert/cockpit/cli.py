"""The cockpit CLI (`uv run dsff cockpit ...`)."""

from __future__ import annotations

import argparse
from pathlib import Path

from dsfforza.convert.cockpit.fit import fit
from dsfforza.convert.cockpit.geometry import GAUGE_UNITS, load_cockpit
from dsfforza.convert.cockpit.previews import check_holes, needle_survey, render, render_gauges

# --- CLI -----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit", help="steering wheel, gauges, mirrors and eye point of a Forza cockpit")
    f.add_argument("car", type=Path, help="extracted car folder")
    f.add_argument("name")
    f.add_argument("--gamedb", type=Path, action="append", default=[])
    r = sub.add_parser("render", help="combined render and head-camera view of a converted car")
    r.add_argument("archive", type=Path)
    r.add_argument("--id", type=int, required=True)
    r.add_argument("--png", type=Path)
    r.add_argument("--eye-view", type=Path)
    r.add_argument("--size", type=int, default=360)
    gp = sub.add_parser("gauges", help="the gauge cluster from the head camera, needles at given readings")
    gp.add_argument("archive", type=Path)
    gp.add_argument("--id", type=int, required=True)
    gp.add_argument("--png", type=Path, required=True)
    gp.add_argument(
        "--at",
        action="append",
        default=[],
        metavar="MPH,RPM",
        help="one tile per reading (default 0,0 30,2000 60,4000 100,6000)",
    )
    gp.add_argument("--fov", type=float, default=28)
    hp = sub.add_parser("holes", help="interior from several head positions and directions; empty pixels magenta")
    hp.add_argument("archive", type=Path)
    hp.add_argument("--id", type=int, action="append", required=True)
    hp.add_argument("--png", type=Path, help="per car: <png stem>-<id>.png")
    ns = sub.add_parser("needles", help="per ported car: needle gauge ids against the hierarchy's gauge records")
    ns.add_argument("archive", type=Path)
    ns.add_argument("--manifest", type=Path, help="default: the archive's .json")
    ns.add_argument("--id", type=int, action="append", default=[])
    ns.add_argument("--tsv", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "needles":
        rows = needle_survey(args.archive, args.manifest, args.id)
        if args.tsv:
            cols = list(rows[0]) if rows else []
            lines = ["\t".join(cols)] + [
                "\t".join(",".join(map(str, v)) if isinstance(v, list) else str(v) for v in r.values()) for r in rows
            ]
            args.tsv.write_text("\n".join(lines) + "\n", encoding="utf-8")
        bad = [r for r in rows if r["unturned"] or r["no_gauge_id_triangles"] or r["parked"]]
        both = sum(1 for r in rows if set(r["needles"]) >= {1, 2} and r not in bad)
        print(
            f"{len(rows)} cars: {both} with both needles turning, "
            f"{sum(1 for r in rows if not r['needles'] and not r['no_gauge_id_triangles'])} without needles"
        )
        for r in bad:
            print(
                f"  {r['spool_id']:4d} {r['media']:30s} host {r['host']:3d}: needles {r['needles']}, "
                f"gauge records {r['gauge_records']}, unturned {r['unturned']}, "
                f"needle triangles without a gauge id {r['no_gauge_id_triangles']}, parked {r['parked']}"
            )
        print(f"{len(bad)} cars with a speed or tach needle that never turns")
        added = [r for r in rows if r["added"]]
        if added:
            print(f"{len(added)} cars got gauge records: " + ", ".join(f"{r['media']} {r['added']}" for r in added))
        return

    if args.cmd == "holes":
        for spool_id in args.id:
            png = args.png.with_name(f"{args.png.stem}-{spool_id}.png") if args.png else None
            r = check_holes(args.archive, spool_id, png)
            print(
                spool_id,
                f"worst {r['worst']} {r['worst_share']:.2%}",
                " ".join(f"{k}={v:.1%}" for k, v in r["views"].items() if v > 0.002),
            )
        return
    if args.cmd == "gauges":
        readings = [tuple(float(x) for x in a.split(",")) for a in args.at] or [
            (0, 0),
            (30, 2000),
            (60, 4000),
            (100, 6000),
        ]
        print(render_gauges(args.archive, args.id, args.png, readings, args.fov))
        return
    if args.cmd == "fit":
        sections = load_cockpit(args.car, args.name)
        if sections is None:
            raise SystemExit(f"no {args.name}_cockpit.carbin in {args.car}")
        cf = fit(sections, args.name, args.gamedb, folder=args.car)

        def r3(v):
            return "(" + ", ".join(f"{x:.3f}" for x in v) + ")"

        print(f"steering wheel: centre {r3(cf.wheel.centre)} axis {r3(cf.wheel.axis)} radius {cf.wheel.radius:.3f}")
        for g in cf.gauges.values():
            print(f"gauge {g.family}: pivot {r3(g.pivot)} normal {r3(g.normal)} sweep {g.sweep_deg:.1f} deg")
            cal = cf.calibration.get(g.family)
            if cal:
                print(
                    f"  DSF id {cal.gauge_id}: {cal.sweep} deg per {GAUGE_UNITS[g.family]:g} "
                    f"{'mph' if g.family == 'speed' else 'rpm'}{' (reversed)' if cal.reverse else ''}, "
                    f"rest turned {cal.rest_deg:+.1f} deg, dial to {cal.dial_max:g}, "
                    f"worst tick {cal.residual_deg:.1f} deg off ({cal.source})"
                )
        if cf.display_faces:
            print(f"digital displays: {cf.display_faces} triangles drawn with their face texture")
        if cf.digital:
            print(f"digital readouts: {cf.digital} triangles, drawn blank")
        for side, p in cf.mirrors.items():
            print(f"mirror {side}: {r3(p)}")
        print(f"eye {r3(cf.eye)} = wheel + offset {r3(cf.offset)} ({cf.offset_source}); Forza space, front -z")
    else:
        print(render(args.archive, args.id, args.png, args.eye_view, args.size))
