"""Does each car's steering wheel turn about its own column? Measured in a built
archive (research/cockpit.md, "The steering wheel turns about its hub").

The engine draws the skinned steering wheel at A + R(v - hinge) and turns R
about the steering marker's direction (its quaternion applied to (0, 0, -1))
through A, the marker plus VO3D p538/p559. In the mesh's own space the wheel
therefore turns about the line through the hinge pivot along the marker's
direction. Per car this fits the rim of the wheel each view draws (the
interior entry's skinned vertices for the cockpit view, the full entry's finest
LOD for the chase views; cockpit.fit_wheel_points) and reports:

- off: the rim centre's distance from that line (the hub circles with this
  radius while steering; 2 off sin(angle / 2) away at an angle);
- tilt: the angle between the marker's direction and the rim's normal (the
  wheel wobbles out of its plane);
- hinge_tilt: the same for the hinge axis (not what the engine turns about,
  checked as well);
- radius: the drawn rim's radius;
- grip: where the driver's hands hold it in the cockpit view. DSF's hand rig grips
  0.176 m from the column, 45 degrees either side of the top, centred on A
  (cockpit.hand_grips; an in-game probe of the arms' fists agrees,
  research/cockpit.md "Hands on Forza's rim"). `grip_stock` is the largest
  distance from those grips to the drawn rim; `grip_asi` the same after
  dsfforza.asi [hands] moves each grip onto the rim the car's VO3D gives it
  (p95 radius, p87 centre along the column; by ASI_MAX_MOVE at most, the
  plugin's default), and `asi_rim` how far that rim is from the drawn one
  (centre distance plus radius difference).

    uv run dsff steering_axle out/final4/dngvehicles.sp --out out/axle/final4.tsv
    uv run dsff steering_axle out/stock/Install/dngvehicles.sp --stock
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import struct
from pathlib import Path

from dsfforza.convert import cockpit

NEAR = 0.3  # m from the hinge: the largest rim (0.24 m) plus the largest hand shift (6 cm)
# dsfforza.asi [hands] (tools/asi/src/mod_hands.cpp) moves a hand that holds the wheel
# by at most this (max_move_mm); the grips modelled here always hold it.
ASI_MAX_MOVE = 0.10


def skinned_points(mesh, skin: bytes) -> dict[int, list[tuple]]:
    """LOD mask -> positions of the vertices bound to the steering wheel."""
    from dsfforza.formats import vehicle_mesh as vm

    by_sub = {}
    for part in mesh.parts:
        for lod in part.lods:
            for n in lod.submodels:
                by_sub.setdefault(n, lod.mask)
    out: dict[int, set] = {}
    for n, sub in enumerate(mesh.submodels):
        vb = mesh.vertex_buffers.get(sub.vertex_buffer)
        e9 = vb and vb.element(9)
        if not e9:
            continue
        pts = out.setdefault(by_sub.get(n, -1), set())
        for _, tri in vm.triangles(mesh, sub):
            for i in tri:
                if bytes(vb.read(e9, i)) == skin:
                    pts.add(vm.position(vb, i))
    return {mask: sorted(p) for mask, p in out.items() if p}


def turn_error(pts: list[tuple], pivot, direction, hinge_axis) -> dict:
    """The fitted rim against the turn line (pivot, direction)."""

    def turn(p):  # DSF <-> Forza orientation (forza2dsf.Placement without the offsets)
        return (-p[0], p[1], -p[2])

    # fit_wheel_points works in Forza space (the driver's end of a modelled column is +z).
    w = cockpit.fit_wheel_points([turn(p) for p in pts])
    w = cockpit.Wheel(turn(w.centre), turn(w.axis), w.radius)
    d = cockpit._norm(direction)
    rel = cockpit._sub(w.centre, pivot)
    along = cockpit._dot(rel, d)
    off = math.sqrt(max(0.0, cockpit._dot(rel, rel) - along * along))

    def angle(a, b):
        return math.degrees(math.acos(min(1.0, abs(cockpit._dot(cockpit._norm(a), cockpit._norm(b))))))

    return {
        "off": off,
        "along": along,
        "tilt": angle(d, w.axis),
        "hinge_tilt": angle(hinge_axis, w.axis),
        "radius": w.radius,
        "centre": w.centre,
        "axis": w.axis,
        "points": len(pts),
    }


def asi_grip(grip, rim) -> tuple:
    """Where dsfforza.asi [hands] moves a grip of the stock rig (on the animations'
    grip circle): onto the nearest point of `rim` (centre, unit axis, radius), by
    ASI_MAX_MOVE at most."""
    centre, axis, radius = rim
    target = cockpit._rim_nearest(grip, centre, axis, radius)
    delta = cockpit._sub(target, grip)
    miss = math.sqrt(cockpit._dot(delta, delta))
    if miss > ASI_MAX_MOVE:
        delta = cockpit._scale(delta, ASI_MAX_MOVE / miss)
    return cockpit._add(grip, delta)


def grip_error(anchor, drawn: dict, asi_rim) -> dict:
    """The hand rig's grips at `anchor` against the drawn rim, without and with
    dsfforza.asi (asi_rim: the rim from the car's VO3D, or None for a car without it)."""
    rim = (drawn["centre"], cockpit._norm(drawn["axis"]), drawn["radius"])

    def miss(p):
        return math.dist(p, cockpit._rim_nearest(p, *rim))

    grips = cockpit.hand_grips(anchor)
    out = {"grip_stock": max(miss(g) for g in grips)}
    out["grip_asi"] = max(miss(asi_grip(g, asi_rim)) for g in grips) if asi_rim else out["grip_stock"]
    if asi_rim:
        out["asi_rim"] = math.dist(asi_rim[0], rim[0]) + abs(asi_rim[2] - rim[2])
    return out


def survey_vehicle(archive: Path, stream, root, spool_id: int) -> dict:
    from dsfforza.formats import upvh
    from dsfforza.formats import vehicle_mesh as vm

    kinds = upvh.entries_for(stream, root.entries, spool_id, None)
    if 0 not in kinds:
        return {"error": "no full entry"}
    data = vm.payload(stream, vm.child(vm.child(kinds[0], "NONR"), "UPVH"))
    host = cockpit.host_cockpit(data)
    h = upvh.parse(data)
    hinge = h.table("hinges")[host.wheel_hinge]
    pivot = struct.unpack_from("<3f", hinge)
    hinge_axis = struct.unpack_from("<3f", hinge, 0x10)
    marker_raw = h.table("markers")[host.wheel_marker]
    q = struct.unpack_from("<4f", marker_raw, 0x10)
    direction = cockpit.quat_rotate(q, (0.0, 0.0, -1.0))
    row: dict = {"pivot": pivot, "direction": direction}
    # The hand rig's anchor A and the rim dsfforza.asi reads (cockpit.GRIP_PARAMS).
    from dsfforza.build import handling

    vo3d = handling.VO3D.parse(vm.payload(stream, vm.child(vm.child(kinds[0], "NONR"), "VO3D")))
    marker = struct.unpack_from("<3f", marker_raw)
    anchor = (marker[0], marker[1] + vo3d.params.get(538, 0.0), marker[2] + vo3d.params.get(559, 0.0))
    asi_rim = None
    radius = vo3d.params.get(cockpit.GRIP_RADIUS_PARAM, 0.0)
    if 0.05 < radius < 0.4:
        engine_dir = cockpit.quat_rotate(q, (0.0, 0.0, 1.0))  # spec + 0x37F0
        along = vo3d.params.get(cockpit.GRIP_ALONG_PARAM, 0.0)
        asi_rim = (cockpit._add(marker, cockpit._scale(engine_dir, along)), cockpit._norm(engine_dir), radius)
    index = {id(e): n for n, e in enumerate(root.entries)}
    for kind, view in ((1, "cockpit"), (0, "chase")):
        if kind not in kinds:
            continue
        _, mesh = vm.load(archive, index[id(kinds[kind])], root.entries)
        lods = skinned_points(mesh, host.wheel_skin)
        if not lods:
            continue
        # The finest LOD: the full entry's masks run 4, 8, 16 (high to low); the interior has one (64).
        mask = min((m for m in lods if m > 1), default=min(lods))
        # A column or boss modelled with the wheel spoils the fit (FG Falcon racers, Mustang
        # Boss 429's main-carbin wheel): only the points near the hinge, as cockpit.fit_main_wheel.
        pts = [p for p in lods[mask] if math.dist(p, pivot) < NEAR]
        if len(pts) < 12:
            continue
        row[view] = turn_error(pts, pivot, direction, hinge_axis)
        if view == "cockpit":
            row[view].update(grip_error(anchor, row[view], asi_rim))
    return row


def ported_cars(manifest: Path) -> list[tuple[int, str, int | None]]:
    d = json.loads(manifest.read_text(encoding="utf-8"))
    return [(c["spool_id"], c["media"], c.get("host")) for c in d["cars"]]


def stock_ids() -> list[tuple[int, str, None]]:
    from dsfforza import REPO

    with (REPO / "research" / "vehicles.tsv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter="\t"))
    seen = {}
    for r in rows:
        seen.setdefault(int(r["spool_id"]), r.get("name") or r.get("model") or "")
    return [(sid, name, None) for sid, name in sorted(seen.items())]


def fmt(v, scale=100.0, digits=1) -> str:
    return "" if v is None else f"{v * scale:.{digits}f}"


def summary(rows: list[dict], view: str) -> None:
    ok = [r for r in rows if view in r]
    if not ok:
        return
    print(f"{view}: {len(ok)} cars")
    for key, label, unit, scale, limits in (
        ("off", "hub off the turn line", "cm", 100.0, (0.005, 0.01, 0.02)),
        ("tilt", "marker direction vs rim normal", "deg", 1.0, (1.0, 2.0, 5.0)),
        ("hinge_tilt", "hinge axis vs rim normal", "deg", 1.0, (1.0, 2.0, 5.0)),
    ):
        col = sorted(r[view][key] for r in ok)
        worst = max(ok, key=lambda r: r[view][key])
        over = ", ".join(f"{sum(1 for v in col if v > lim)} over {lim * scale:g}" for lim in limits)
        print(
            f"  {label}: median {col[len(col) // 2] * scale:.2f} {unit}, 90th pct "
            f"{col[int(0.9 * (len(col) - 1))] * scale:.2f}, worst {col[-1] * scale:.2f} ({worst['name']}); {over}"
        )
    radii = [r[view]["radius"] for r in ok]
    print(f"  rim radius: median {statistics.median(radii):.3f} m, {min(radii):.3f} to {max(radii):.3f}")
    for key, label in (
        ("grip_stock", "grips off the rim, stock hands"),
        ("grip_asi", "grips off the rim, with dsfforza.asi [hands]"),
        ("asi_rim", "dsfforza.asi's rim vs the drawn rim"),
    ):
        have = [r for r in ok if key in r[view]]
        if not have:
            continue
        col = sorted(r[view][key] for r in have)
        worst = max(have, key=lambda r: r[view][key])
        over = ", ".join(f"{sum(1 for v in col if v > lim)} over {lim * 100:g}" for lim in (0.005, 0.01, 0.02))
        print(
            f"  {label} ({len(col)} cars): median {col[len(col) // 2] * 100:.2f} cm, 90th pct "
            f"{col[int(0.9 * (len(col) - 1))] * 100:.2f}, worst {col[-1] * 100:.2f} ({worst['name']}); {over}"
        )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("archive", type=Path, help="dngvehicles.sp (built, or the stock one with --stock)")
    ap.add_argument("--manifest", type=Path, help="port_batch manifest (default: <archive>.json)")
    ap.add_argument("--stock", action="store_true", help="the stock fleet (research/vehicles.tsv)")
    ap.add_argument("--ids", help="comma-separated spool IDs only")
    ap.add_argument("--out", type=Path, help="TSV")
    args = ap.parse_args(argv)
    from dsfforza.formats.chnk import read_chunk

    cars = stock_ids() if args.stock else ported_cars(args.manifest or args.archive.with_suffix(".json"))
    if args.ids:
        keep = {int(x) for x in args.ids.split(",")}
        cars = [c for c in cars if c[0] in keep]
    rows = []
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        for sid, name, host in cars:
            try:
                row = survey_vehicle(args.archive, stream, root, sid)
            except Exception as error:  # a stock car without a steering marker, ...
                row = {"error": f"{type(error).__name__}: {error}"}
            row.update({"id": sid, "name": name, "host": host})
            rows.append(row)
            if "error" in row:
                print(f"{sid:5} {name[:30]:30} {row['error']}")
                continue
            parts = []
            for view in ("cockpit", "chase"):
                if view in row:
                    v = row[view]
                    grip = f" grip {fmt(v['grip_stock'])} -> {fmt(v['grip_asi'])} cm" if "grip_stock" in v else ""
                    parts.append(
                        f"{view} off {fmt(v['off'])} cm tilt {v['tilt']:.1f} (hinge {v['hinge_tilt']:.1f}) "
                        f"r {v['radius']:.3f}{grip}"
                    )
            print(f"{sid:5} {name[:30]:30} " + "  ".join(parts))
    print()
    for view in ("cockpit", "chase"):
        summary(rows, view)
    errors = [r for r in rows if "error" in r]
    if errors:
        print(f"{len(errors)} skipped")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        measures = ("off", "along", "tilt", "hinge_tilt", "radius", "grip_stock", "grip_asi", "asi_rim")
        keys = ["id", "name", "host", "error"] + [f"{view}_{k}" for view in ("cockpit", "chase") for k in measures]
        with args.out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, keys, delimiter="\t", extrasaction="ignore")
            w.writeheader()
            for r in rows:
                flat = {k: r.get(k) for k in ("id", "name", "host", "error")}
                for view in ("cockpit", "chase"):
                    for k in measures:
                        if view in r and k in r[view]:
                            flat[f"{view}_{k}"] = round(r[view][k], 5)
                w.writerow(flat)


if __name__ == "__main__":
    main()
