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
- shift: how far A is from the hinge, so how far the engine draws the wheel (and the
  hands with it) from where its mesh puts it. The engine keeps the last steering
  marker for A and the head camera hangs from the first (research/cockpit.md, "Hosts
  with two steering markers"); `eye_off` is the head camera's sideways distance from
  the hub the cockpit view draws;
- grip: where the driver's hands hold it in the cockpit view. DSF's hand rig grips
  0.176 m from the column, 45 degrees either side of the top, centred on A
  (cockpit.hand_grips; an in-game probe of the arms' fists agrees,
  research/cockpit.md "Hands on Forza's rim"). `grip_vanilla` is the largest
  distance from those grips to the drawn rim; `grip_asi` the same after
  dsfforza.asi [hands] moves each grip onto the rim the car's VO3D gives it
  (p95 radius, p87 centre along the column; by ASI_MAX_MOVE at most, the
  plugin's default), and `asi_rim` how far that rim is from the drawn one
  (centre distance plus radius difference).
- the chase views' grips: the driver there is a full-body occupant posed in the
  driver seat marker's frame, his grips the same circle about the vanilla
  steering marker's place in that frame (rig_grips). The same `grip_vanilla` and
  `grip_asi` (dsfforza.asi [hands] moves him too since issue #117), and
  `turn_off`, how far from the rim's column the animations turn his hands
  (the plugin keeps each hand's angle about that centre, so they turn with the
  wheel, and moves them by up to `asi_move` over a full turn).

    uv run dsff steering_axle out/final4/dngvehicles.sp --out out/axle/final4.tsv
    uv run dsff steering_axle out/vanilla/Install/dngvehicles.sp --vanilla
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
ASI_MAX_MOVE = 0.12
# DSF's full lock: the steering matrix turns the cockpit arms by up to 1.7456 rad (100 deg)
# (research/cockpit.md, "Driver's hands and the steering wheel").
TURN_DEG = 105


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


def asi_target(grip, rim, about) -> tuple:
    """Where dsfforza.asi [hands] puts a grip of the vanilla rig, which the animations turn
    on a circle about `about`: on `rim` (centre, unit axis, radius) at the grip's angle
    about `about` (for the cockpit arms `about` is on the column: the nearest point)."""
    centre, axis, radius = rim
    d = cockpit._sub(grip, about)
    d = cockpit._sub(d, cockpit._scale(axis, cockpit._dot(d, axis)))
    return cockpit._add(centre, cockpit._scale(cockpit._norm(d), radius))


def asi_grip(grip, rim, about) -> tuple:
    """asi_target, by ASI_MAX_MOVE at most."""
    delta = cockpit._sub(asi_target(grip, rim, about), grip)
    miss = math.sqrt(cockpit._dot(delta, delta))
    if miss > ASI_MAX_MOVE:
        delta = cockpit._scale(delta, ASI_MAX_MOVE / miss)
    return cockpit._add(grip, delta)


def rig_grips(anchor, column, seat=None) -> tuple[list[tuple], tuple]:
    """The vanilla rig's grips at rest and the axis the steering turns them about (unit).
    Cockpit: about `anchor` in the car's axes, turned by the steering matrix about the
    marker's direction (`column`). Chase views (`seat` = (position, quaternion) of the
    driver seat marker): the full-body occupant is posed in that marker's frame and grips
    the same circle about the vanilla steering marker's place there
    (cockpit.VANILLA_MARKER_FROM_SEAT); his animations turn it about the vanilla column."""
    if seat is None:
        return cockpit.hand_grips(anchor), cockpit._norm(column)
    pos, quat = seat
    grips = [
        cockpit._add(pos, cockpit.quat_rotate(quat, g)) for g in cockpit.hand_grips(cockpit.VANILLA_MARKER_FROM_SEAT)
    ]
    return grips, cockpit._norm(cockpit.quat_rotate(quat, cockpit.VANILLA_COLUMN))


def grip_error(anchor, drawn: dict, asi_rim, seat=None) -> dict:
    """The hand rig's grips about `anchor` against the drawn rim, without and with
    dsfforza.asi (asi_rim: the rim from the car's VO3D, or None for a car without it).
    `turn_off`: how far from the drawn rim's column the animations turn the grips
    (about `anchor`), in the rim's plane. `asi_move`: the largest move the plugin makes
    to a grip, at rest or turned up to TURN_DEG either way (15 degree steps)."""
    rim = (drawn["centre"], cockpit._norm(drawn["axis"]), drawn["radius"])

    def miss(p):
        return math.dist(p, cockpit._rim_nearest(p, *rim))

    grips, column = rig_grips(anchor, asi_rim[1] if asi_rim else rim[1], seat)
    d = cockpit._sub(anchor, rim[0])
    d = cockpit._sub(d, cockpit._scale(rim[1], cockpit._dot(d, rim[1])))
    out = {"grip_vanilla": max(miss(g) for g in grips), "turn_off": math.sqrt(cockpit._dot(d, d))}
    if not asi_rim:
        out["grip_asi"] = out["grip_vanilla"]
        return out
    out["grip_asi"] = max(miss(asi_grip(g, asi_rim, anchor)) for g in grips)
    out["asi_rim"] = math.dist(asi_rim[0], rim[0]) + abs(asi_rim[2] - rim[2])
    moves = []
    for step in range(-TURN_DEG // 15, TURN_DEG // 15 + 1):
        h = math.radians(7.5 * step)
        q = (*cockpit._scale(column, math.sin(h)), math.cos(h))
        for g in grips:
            turned = cockpit._add(anchor, cockpit.quat_rotate(q, cockpit._sub(g, anchor)))
            moves.append(math.dist(turned, asi_target(turned, asi_rim, anchor)))
    out["asi_move"] = max(moves)
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
    # The engine keeps the last steering marker for the wheel and the hands (FUN_0088E310).
    marker_raw = h.table("markers")[host.engine_marker]
    q = struct.unpack_from("<4f", marker_raw, 0x10)
    direction = cockpit.quat_rotate(q, (0.0, 0.0, -1.0))
    row: dict = {"pivot": pivot, "direction": direction}
    # The hand rig's anchor A and the rim dsfforza.asi reads (cockpit.GRIP_PARAMS).
    from dsfforza.build import handling

    vo3d_data = vm.payload(stream, vm.child(vm.child(kinds[0], "NONR"), "VO3D"))
    vo3d = handling.VO3D.parse(vo3d_data)
    marker = struct.unpack_from("<3f", marker_raw)
    anchor = (marker[0], marker[1] + vo3d.params.get(538, 0.0), marker[2] + vo3d.params.get(559, 0.0))
    # The wheel is drawn at A + R(v - hinge): `shift` from where the mesh has it. The
    # hands go with A, so everything below is measured in the mesh's frame (minus shift).
    shift = cockpit._sub(anchor, pivot)
    asi_rim = None
    radius = vo3d.params.get(cockpit.GRIP_RADIUS_PARAM, 0.0)
    if 0.05 < radius < 0.4:
        engine_dir = cockpit.quat_rotate(q, (0.0, 0.0, 1.0))  # spec + 0x37F0
        along = vo3d.params.get(cockpit.GRIP_ALONG_PARAM, 0.0)
        centre = cockpit._sub(cockpit._add(marker, cockpit._scale(engine_dir, along)), shift)
        asi_rim = (centre, cockpit._norm(engine_dir), radius)
    # The head camera hangs from the first steering marker (FUN_0088D3B0).
    eye, _ = cockpit.head_camera(data, vo3d_data)
    # The chase views' driver sits on the driver seat marker (role 10, cockpit.patch_seats).
    seats = cockpit.seat_markers(data)
    seat = None
    if cockpit.DRIVER_SEAT_ROLE in seats:
        raw = h.table("markers")[seats[cockpit.DRIVER_SEAT_ROLE]]
        seat = (cockpit._sub(struct.unpack_from("<3f", raw), shift), struct.unpack_from("<4f", raw, 0x10))
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
        row[view]["shift"] = math.dist(anchor, pivot)
        if view == "cockpit":
            # Sideways from the head camera to the hub the view draws.
            row[view]["eye_off"] = abs(eye[0] - (row[view]["centre"][0] + shift[0]))
            row[view].update(grip_error(pivot, row[view], asi_rim))
        elif seat is not None:
            pos, quat = seat
            at = cockpit._add(pos, cockpit.quat_rotate(quat, cockpit.VANILLA_MARKER_FROM_SEAT))
            row[view].update(grip_error(at, row[view], asi_rim, seat))
    return row


def ported_cars(manifest: Path) -> list[tuple[int, str, int | None]]:
    d = json.loads(manifest.read_text(encoding="utf-8"))
    return [(c["spool_id"], c["media"], c.get("host")) for c in d["cars"]]


def vanilla_ids() -> list[tuple[int, str, None]]:
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
        ("shift", "wheel drawn off its modelled place (A - hinge)", "cm", 100.0, (0.005, 0.01, 0.05)),
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
        ("eye_off", "head camera sideways off the drawn hub"),
        ("grip_vanilla", "grips off the rim, vanilla hands"),
        ("grip_asi", "grips off the rim, with dsfforza.asi [hands]"),
        ("asi_rim", "dsfforza.asi's rim vs the drawn rim"),
        ("turn_off", "the hands' turn centre off the drawn rim's column"),
        ("asi_move", "dsfforza.asi's largest move of a hand, up to full lock"),
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
    ap.add_argument("archive", type=Path, help="dngvehicles.sp (built, or the vanilla one with --vanilla)")
    ap.add_argument("--manifest", type=Path, help="port_batch manifest (default: <archive>.json)")
    ap.add_argument("--vanilla", action="store_true", help="the vanilla fleet (research/vehicles.tsv)")
    ap.add_argument("--ids", help="comma-separated spool IDs only")
    ap.add_argument("--out", type=Path, help="TSV")
    args = ap.parse_args(argv)
    from dsfforza.formats.chnk import read_chunk

    cars = vanilla_ids() if args.vanilla else ported_cars(args.manifest or args.archive.with_suffix(".json"))
    if args.ids:
        keep = {int(x) for x in args.ids.split(",")}
        cars = [c for c in cars if c[0] in keep]
    rows = []
    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        for sid, name, host in cars:
            try:
                row = survey_vehicle(args.archive, stream, root, sid)
            except Exception as error:  # a vanilla car without a steering marker, ...
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
                    grip = f" grip {fmt(v['grip_vanilla'])} -> {fmt(v['grip_asi'])} cm" if "grip_vanilla" in v else ""
                    eye = f" eye {fmt(v['eye_off'])} cm" if "eye_off" in v else ""
                    parts.append(
                        f"{view} off {fmt(v['off'])} cm tilt {v['tilt']:.1f} (hinge {v['hinge_tilt']:.1f}) "
                        f"shift {fmt(v['shift'])} cm{eye} r {v['radius']:.3f}{grip}"
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
        measures = (
            "off",
            "along",
            "tilt",
            "hinge_tilt",
            "shift",
            "eye_off",
            "radius",
            "grip_vanilla",
            "grip_asi",
            "asi_rim",
            "turn_off",
            "asi_move",
        )
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
