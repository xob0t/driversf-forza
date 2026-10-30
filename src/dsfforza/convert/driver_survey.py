"""Where the driver model's hands and head land in the chase views, per car, with
the converter's old and new driver-seat placement (research/cockpit.md, "The
driver's seat follows the wheel").

The driver model sits on the driver-seat marker (role 10) and holds the wheel
where vanilla cars have it in the seat's frame (cockpit.VANILLA_MARKER_FROM_SEAT).
For each Forza car (offline, from its extracted folder in out/cache and its host
in the vanilla archive) this measures:

- off: how far the steering anchor (the hinge/marker at the Forza wheel centre plus
  the hand shift) is from the vanilla point in the seat's frame (old seat: under the
  Forza eye with the host's eye-to-seat offset; new seat: cockpit.seat_for_marker);
- miss: the largest distance from the driver model's grips (the vanilla hand rig at
  the vanilla point of the seat) to the drawn rim (the cockpit wheel fit, onto which
  the main-carbin wheel is aligned);
- roof: the room over the driver model's eyes (seat + cockpit.FLEET_EYE_ABOVE_SEAT)
  up to the first triangle of the Forza body at LOD 1 straight above.

    uv run dsff driver_survey cars --list tools/batches/testdrive.txt --hosts out/testdrive11/work
    uv run dsff driver_survey vanilla      # roof room of the vanilla fleet, for comparison

`--hosts` is a folder of port_batch per-car reports (<media>.json with "host").
Writes a TSV (--out) and prints the summary.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import struct
from pathlib import Path

from dsfforza import REPO
from dsfforza.convert import cockpit, forza2dsf

VANILLA_ARCHIVE = REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"
CACHE = REPO / "out" / "cache"
ROOF_LOD = 1
SIDE = forza2dsf.Placement(0.0, 0.0)  # the turn only: every measure here is relative


def host_seat(stream, root, host: int) -> dict | None:
    """The host's driver-seat marker (position, quaternion) and head-camera eye."""
    from dsfforza.build import handling
    from dsfforza.formats import upvh
    from dsfforza.formats.vehicle_mesh import child, payload

    kinds = upvh.entries_for(stream, root.entries, host, None)
    if 0 not in kinds:
        return None
    nonr = child(kinds[0], "NONR")
    data = payload(stream, child(nonr, "UPVH"))
    slots = cockpit.seat_markers(data)
    if cockpit.DRIVER_SEAT_ROLE not in slots:
        return None
    raw = upvh.parse(data).table("markers")[slots[cockpit.DRIVER_SEAT_ROLE]]
    vo3d = handling.VO3D.parse(payload(stream, child(nonr, "VO3D")))
    eye, marker = cockpit.head_camera(data, payload(stream, child(nonr, "VO3D")))
    return {
        "pos": struct.unpack_from("<3f", raw),
        "quat": struct.unpack_from("<4f", raw, 0x10),
        "eye": eye,
        "marker": marker,
        "anchor": (marker[0], marker[1] + vo3d.get(538), marker[2] + vo3d.get(559)),
    }


def quat_inverse(q):
    return (-q[0], -q[1], -q[2], q[3])


def roof_room(point, tris) -> float | None:
    """Distance straight up from `point` to the nearest triangle above it."""
    px, py, pz = point
    best = None
    for a, b, c in tris:
        if max(a[1], b[1], c[1]) <= py:
            continue
        if min(a[0], b[0], c[0]) > px or max(a[0], b[0], c[0]) < px:
            continue
        if min(a[2], b[2], c[2]) > pz or max(a[2], b[2], c[2]) < pz:
            continue
        # barycentric in the xz plane
        d = (b[2] - c[2]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[2] - c[2])
        if abs(d) < 1e-12:
            continue
        u = ((b[2] - c[2]) * (px - c[0]) + (c[0] - b[0]) * (pz - c[2])) / d
        v = ((c[2] - a[2]) * (px - c[0]) + (a[0] - c[0]) * (pz - c[2])) / d
        w = 1 - u - v
        if u < -1e-9 or v < -1e-9 or w < -1e-9:
            continue
        y = u * a[1] + v * b[1] + w * c[1]
        if y > py and (best is None or y - py < best):
            best = y - py
    return best


def body_triangles(car, lod: int) -> list:
    tris = []
    for s in car.body:
        for sub in s.subsections:
            if sub.lod != lod:
                continue
            pool = s.vertices_for(sub)
            for t in sub.triangles():
                tris.append(tuple(SIDE.point(pool[i].position) for i in t))
    return tris


def chase_miss(seat, quat, centre, axis, radius) -> float:
    """Largest distance from the driver model's grips to the drawn rim."""
    anchor = cockpit._add(seat, cockpit.quat_rotate(quat, cockpit.VANILLA_MARKER_FROM_SEAT))
    return cockpit.hand_grip_error(anchor, centre, axis, radius)


def survey_car(media: str, host: int, stream, root) -> dict:
    row = {"media": media, "host": host}
    folder = next((CACHE / g / media for g in ("fh1", "fm4") if (CACHE / g / media).is_dir()), None)
    if folder is None:
        row["error"] = "not in out/cache"
        return row
    game = folder.parent.name
    seat_info = host_seat(stream, root, host)
    if seat_info is None:
        row["error"] = "host has no driver seat marker"
        return row
    car = forza2dsf.load_car(folder, media)
    gamedb = REPO / "out" / "db" / f"{game}_gamedb.slt"
    cf = cockpit.fit(
        cockpit.load_cockpit(folder, media) or [],
        media,
        [gamedb] if gamedb.is_file() else [],
        folder=folder,
        main_sections=car.sections,
    )
    centre = SIDE.point(cf.wheel.centre)
    axis = cockpit._norm(SIDE.normal(cf.wheel.axis))
    radius = cf.wheel.radius
    pivot = cockpit.seat_anchor(centre, axis, radius)  # where patch_seats puts the seat
    eye = SIDE.point(cf.eye)
    quat = seat_info["quat"]
    # Old placement (cockpit.patch_seats before): under the Forza eye, host's eye-to-seat offset.
    old = cockpit._sub(eye, cockpit._sub(seat_info["eye"], seat_info["pos"]))
    old = (eye[0], old[1], old[2])
    new = cockpit.seat_for_marker(pivot, quat)
    tris = body_triangles(car, ROOF_LOD)
    inv = quat_inverse(quat)
    for tag, seat in (("old", old), ("new", new)):
        off = cockpit._sub(cockpit.quat_rotate(inv, cockpit._sub(pivot, seat)), cockpit.VANILLA_MARKER_FROM_SEAT)
        row[f"off_{tag}"] = math.sqrt(cockpit._dot(off, off))
        row[f"off_{tag}_xyz"] = tuple(round(c, 4) for c in off)
        row[f"miss_{tag}"] = chase_miss(seat, quat, centre, axis, radius)
        body_eye = cockpit._add(seat, cockpit.quat_rotate(quat, cockpit.FLEET_EYE_ABOVE_SEAT))
        row[f"roof_{tag}"] = roof_room(body_eye, tris)
    row["head_move"] = tuple(round(n - o, 4) for n, o in zip(new, old))
    return row


def read_list(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.split("@", 1)[0].strip())
    return out


def read_hosts(folder: Path) -> dict[str, int]:
    hosts = {}
    for p in folder.glob("*.json"):
        if p.name.endswith(".job.json"):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("host") is not None and d.get("media"):
            hosts[d["media"]] = int(d["host"])
    return hosts


def fmt(v, scale=100.0, digits=1):
    return "" if v is None else f"{v * scale:.{digits}f}"


def summary(rows: list[dict]) -> None:
    ok = [r for r in rows if "error" not in r]
    print(f"{len(ok)} cars measured, {len(rows) - len(ok)} skipped")
    for key, label in (("off", "anchor off the vanilla point"), ("miss", "grips off the drawn rim")):
        for tag in ("old", "new"):
            col = sorted(r[f"{key}_{tag}"] for r in ok)
            if not col:
                continue
            worst = max(ok, key=lambda r: r[f"{key}_{tag}"])
            over3 = sum(1 for v in col if v > 0.03)
            print(
                f"  {label} ({tag} seat): median {fmt(statistics.median(col))} cm, "
                f"90th pct {fmt(col[int(0.9 * (len(col) - 1))])}, worst {fmt(col[-1])} cm ({worst['media']}), "
                f"{over3} over 3 cm"
            )
    for tag in ("old", "new"):
        col = sorted(r[f"roof_{tag}"] for r in ok if r[f"roof_{tag}"] is not None)
        if col:
            low = min((r for r in ok if r[f"roof_{tag}"] is not None), key=lambda r: r[f"roof_{tag}"])
            print(
                f"  room over the driver's eyes ({tag} seat): median {fmt(statistics.median(col))} cm, "
                f"lowest {fmt(col[0])} cm ({low['media']}), {sum(1 for v in col if v < 0.15)} under 15 cm"
            )


def cmd_cars(args) -> None:
    from dsfforza.formats.chnk import read_chunk

    cars = read_list(args.list) if args.list else []
    cars += args.car or []
    hosts = read_hosts(args.hosts)
    rows = []
    with args.vanilla.open("rb") as stream:
        root = read_chunk(stream)
        for n, media in enumerate(cars, 1):
            if media not in hosts:
                rows.append({"media": media, "error": "no host in --hosts"})
                continue
            try:
                row = survey_car(media, hosts[media], stream, root)
            except Exception as error:  # noqa: BLE001 - one car's failure is a row of the survey
                row = {"media": media, "host": hosts[media], "error": f"{type(error).__name__}: {error}"}
            rows.append(row)
            if "error" in row:
                print(f"{n:4} {media:32s} {row['error']}")
            else:
                print(
                    f"{n:4} {media:32s} host {row['host']:3}  off {fmt(row['off_old'])} -> {fmt(row['off_new'])} cm  "
                    f"miss {fmt(row['miss_old'])} -> {fmt(row['miss_new'])} cm  "
                    f"roof {fmt(row['roof_old'])} -> {fmt(row['roof_new'])} cm"
                )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        keys = (
            ["media", "host", "error"]
            + [f"{k}_{t}" for k in ("off", "miss", "roof") for t in ("old", "new")]
            + ["off_old_xyz", "head_move"]
        )
        with args.out.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, keys, delimiter="\t", extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow(r)
    summary(rows)


def cmd_vanilla(args) -> None:
    """The steering marker in the driver seat's frame and the room over the driver
    model's eyes (full entry, LOD mask 4) in the vanilla fleet."""
    from dsfforza.formats import upvh, vehicle_mesh
    from dsfforza.formats.chnk import read_chunk

    with (REPO / "research" / "vehicles.tsv").open(encoding="utf-8") as f:
        ids = sorted({int(r["spool_id"]) for r in csv.DictReader(f, delimiter="\t")})
    rooms, off = [], []
    with args.vanilla.open("rb") as stream:
        root = read_chunk(stream)
        index = {id(e): n for n, e in enumerate(root.entries)}
        for sid in ids:
            info = host_seat(stream, root, sid)
            if info is None:
                continue
            vehc = upvh.entries_for(stream, root.entries, sid, None)[0]
            _, mesh = vehicle_mesh.load(args.vanilla, index[id(vehc)], root.entries)
            tris = []
            for part_no, part in enumerate(mesh.parts):
                if part_no:  # part 0 is the body; other parts sit in their own frames
                    continue
                for lod in part.lods:
                    if lod.mask != 4:
                        continue
                    for n in lod.submodels:
                        for vb, idx in vehicle_mesh.triangles(mesh, mesh.submodels[n]):
                            tris.append(tuple(vehicle_mesh.position(vb, i) for i in idx))
            eye = cockpit._add(info["pos"], cockpit.quat_rotate(info["quat"], cockpit.FLEET_EYE_ABOVE_SEAT))
            room = roof_room(eye, tris)
            rooms.append((room, sid))
            frame = cockpit.quat_rotate(quat_inverse(info["quat"]), cockpit._sub(info["marker"], info["pos"]))
            off.append((math.dist(frame, cockpit.VANILLA_MARKER_FROM_SEAT), sid, frame))
            print(
                f"{sid:4} marker in the seat frame ({', '.join(f'{c:.3f}' for c in frame)}), "
                f"room over the driver's eyes {fmt(room)} cm"
            )
    odd = [(sid, tuple(round(c, 3) for c in frame)) for d, sid, frame in off if d > 0.01]
    print(
        f"{len(off) - len(odd)} of {len(off)} vanilla cars have the marker within 1 cm of {cockpit.VANILLA_MARKER_FROM_SEAT}"
    )
    print(f"  the others: {odd}")
    col = sorted(r for r, _ in rooms if r is not None)
    print(
        f"{len(col)} vanilla cars: median {fmt(statistics.median(col))} cm, 5th pct {fmt(col[len(col) // 20])}, "
        f"lowest {fmt(col[0])} cm ({min(r for r in rooms if r[0] is not None)[1]}); "
        f"{sum(1 for r, _ in rooms if r is None)} with nothing above"
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cars", help="Forza cars: old and new seat")
    c.add_argument("--list", type=Path, help="batch list (tools/batches/*.txt)")
    c.add_argument("--car", action="append", help="media name (repeatable)")
    c.add_argument("--hosts", type=Path, required=True, help="folder of port_batch per-car reports")
    c.add_argument("--vanilla", type=Path, default=VANILLA_ARCHIVE)
    c.add_argument("--out", type=Path)
    c.set_defaults(func=cmd_cars)
    s = sub.add_parser("vanilla", help="vanilla fleet: room over the driver's eyes")
    s.add_argument("--vanilla", type=Path, default=VANILLA_ARCHIVE)
    s.set_defaults(func=cmd_vanilla)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
