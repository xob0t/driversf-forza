"""How each wheel of a built car sits in its arch, as the game draws it (research/wheels.md,
"Wheel fit").

For every wheel of a car in a built archive (`wheel_clearance.load_car`: the clean exterior
entry's body, the drawn wheel part's radius, width and tyre centre, the hub table, the VO3D):

- the wheel at rest: the hub moved up by the engine's static wheel offset
  (`wheel_clearance.rest_offsets`, the model `forza2dsf.settle_wheels` uses), plus the tyre's
  centre offset;
- its arch as it is seen from the side: the body (sampled every ARCH_CELL / 2) as an
  orthographic side view, per ARCH_CELL square the outermost surface, the tyre a disc at its
  outer face hiding what lies inside it and inboard of that face (side_depth); along
  each ray from the tyre's centre (ARCH_BIN_DEG apart, ARCH_RAY_DEG behind to ahead of straight
  up, from ARCH_INNER to ARCH_REACH tyre radii) the arch's edge is where the visible surface
  first steps out from the wheel well to the fender, at half the step (step_edge, arch_edge;
  the first climb at least half as high as the ray's biggest, so a wider body farther out,
  the cabin's sides of a mid-engined car, isn't taken for the fender). That is the
  outline the eye sees; the nearest body point per ray was often the well liner, inside it, and
  read the S7 04's arch top 43 mm low against the game. A circle fitted to the edge points
  (least squares), the rays cast again from its centre, ARCH_ROUNDS rounds;
- the measures:
  - `fore_aft_mm`: the arch centre ahead (+) or behind (-) the tyre's centre, along the car:
    the median midpoint of the arch's front and rear edges (arch_sides) at heights from 0.3 to
    0.9 tyre radii above the tyre's centre;
  - `top_gap_mm`: from the tyre's top to the arch's edge straight above its centre; negative
    is a tyre over the edge;
  - `lateral_mm`: the tyre's outer face outboard (+) or inboard (-) of the fender line: the
    outermost crest of the arch's edge step on the rays within ARCH_LIP_DEG of straight up
    from the tyre's centre;
  - `arch_up_mm`, `arch_radius`, `lip_rms_mm`, `lips`: the fitted circle's centre above the
    tyre's centre, its radius, the edge points' spread about it (a large one: no round arch),
    their number.

These are the measurement's geometry, not rules of the converter: the ray fan covers a round
arch's opening without reaching the bumpers, the cells are as fine as the body's sampling
needs. Vanilla cars measured the same way give each measure's spread (`survey` on the vanilla
archive), the yardstick a port is compared with. Checked against in-game shots
(research/wheels.md, "Wheel fit").

Usage:
    uv run dsff wheel_fit car out/build/dngvehicles.sp 644 [--report out/build/batch-report.json]
    uv run dsff wheel_fit survey out/vanilla/Install/dngvehicles.sp --out out/wf/vanilla.tsv
    uv run dsff wheel_fit survey out/build/dngvehicles.sp --report out/build/batch-report.json --out out/wf/ports.tsv
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, fields
from pathlib import Path

import numpy as np

from dsfforza.convert import wheel_clearance as wc

ARCH_RAY_DEG = 70.0  # the lip rays, either side of straight up
ARCH_BIN_DEG = 5.0
ARCH_ROUNDS = 4
ARCH_MIN_LIPS = 8  # fewer lip points: no arch fitted
ARCH_REACH = 2.5  # rays out to this many tyre radii from the tyre's centre
ARCH_INNER = 0.6  # ... from this share of the radius (inside: hubs, calipers)
ARCH_LIP_DEG = 60.0  # the fender line: the arch edge within this of straight up
ARCH_CELL = 0.005  # m: the side view's depth cells
ARCH_SIDE_HEIGHTS = (0.3, 0.91, 0.05)  # fore/aft: the arch's sides at these heights (tyre radii above its centre)


@dataclass
class Fit:
    media: str
    id: int
    wheel: str
    radius: float
    width: float
    rest_up_mm: float
    fore_aft_mm: float | None
    arch_up_mm: float | None
    arch_radius: float | None
    top_gap_mm: float | None
    lateral_mm: float | None
    lip_rms_mm: float | None
    lips: int

    def cells(self) -> list[str]:
        out = []
        for f in fields(self):
            v = getattr(self, f.name)
            out.append(
                ""
                if v is None
                else f"{v:.4f}"
                if f.name in ("radius", "width", "arch_radius")
                else f"{v:.1f}"
                if isinstance(v, float)
                else str(v)
            )
        return out


HEADER = [f.name for f in fields(Fit)]


def side_depth(pts: np.ndarray, centre: np.ndarray, outboard: float, extent: float, radius: float, face: float):
    """The wheel and the body round it as seen from the wheel's side, orthographic: per
    ARCH_CELL square of the side view (forward, up) round `centre`, the outermost x (times
    `outboard`) of what is there, NaN where nothing is. The tyre is a disc of `radius` at its
    outer face's x (`face`, times `outboard`) that hides the body points inside it and inboard
    of that face (the wheel-well liner, suspension). Returns (depth grid, its origin)."""
    n = int(math.ceil(2 * extent / ARCH_CELL))
    origin = np.array([centre[2] - extent, centre[1] - extent])
    x = pts[:, 0] * outboard
    hidden = (np.hypot(pts[:, 2] - centre[2], pts[:, 1] - centre[1]) < radius) & (x < face)
    ij = np.floor((np.c_[pts[:, 2], pts[:, 1]] - origin) / ARCH_CELL).astype(int)
    ok = (ij >= 0).all(1) & (ij < n).all(1) & ~hidden
    depth = np.full((n, n), -np.inf)
    np.maximum.at(depth, (ij[ok, 0], ij[ok, 1]), x[ok])
    gf, gu = np.meshgrid(
        origin[0] + (np.arange(n) + 0.5) * ARCH_CELL, origin[1] + (np.arange(n) + 0.5) * ARCH_CELL, indexing="ij"
    )
    tyre = np.hypot(gf - centre[2], gu - centre[1]) < radius
    depth[tyre] = np.maximum(depth[tyre], face)
    depth[np.isinf(depth)] = np.nan
    return depth, origin


def step_edge(v: np.ndarray):
    """(edge index, crest value) of the first step out along a depth profile (NaN: nothing
    there): the profile split into climbs (runs where the surface keeps stepping out), the
    first climb that rises at least half as much as the profile's biggest one, and in it the
    first cell at least halfway up; the crest is the climb's top, the fender's surface. None
    without a climb."""
    idx = np.flatnonzero(np.isfinite(v))
    if len(idx) < 2:
        return None
    w = v[idx]
    climbs, start = [], 0
    for k in range(1, len(w) + 1):
        if k == len(w) or w[k] < w[k - 1]:
            if k - 1 > start:
                climbs.append((start, k - 1, w[k - 1] - w[start]))
            start = k
    if not climbs:
        return None
    biggest = max(c[2] for c in climbs)
    if biggest <= 0:
        return None
    a, b, rise = next(c for c in climbs if c[2] >= 0.5 * biggest)
    half = w[a] + rise / 2
    k = a + int(np.flatnonzero(w[a : b + 1] >= half)[0])
    return int(idx[k]), float(w[b])


def arch_edge(depth, origin, centre2, radius: float, angles) -> np.ndarray:
    """Per ray from `centre2` (forward, up) at each angle (degrees from straight up, + forward),
    the arch's visible edge, from ARCH_INNER to ARCH_REACH tyre radii out: the first step from
    the wheel well out to the fender, at half its height (step_edge). Rows (forward, up, the
    fender's x at the step's crest, the edge's x)."""
    n = depth.shape[0]
    radii = np.arange(ARCH_INNER * radius, ARCH_REACH * radius, ARCH_CELL)
    out = []
    for a in np.radians(angles):
        f = centre2[0] + radii * math.sin(a)
        u = centre2[1] + radii * math.cos(a)
        i = np.floor((f - origin[0]) / ARCH_CELL).astype(int)
        j = np.floor((u - origin[1]) / ARCH_CELL).astype(int)
        ok = (i >= 0) & (i < n) & (j >= 0) & (j < n)
        v = np.full(len(radii), np.nan)
        v[ok] = depth[i[ok], j[ok]]
        found = step_edge(v)
        if found is None:
            continue  # no step out along the ray: no opening here
        k, crest = found
        out.append((f[k], u[k], crest, v[k]))
    return np.array(out).reshape(-1, 4)


def arch_sides(depth, origin, centre2, radius: float, heights) -> np.ndarray:
    """The arch's front and rear edges at each height (m above `centre2`, the tyre's centre in
    the side view (forward, up)): going forward and back from the tyre's centre line, the step
    out to the fender at half its height (arch_edge's rule). Rows (height, front edge, rear
    edge), forward coordinates; rows without both edges are left out."""
    n = depth.shape[0]
    out = []
    reach = np.arange(0.0, ARCH_REACH * radius, ARCH_CELL)
    for h in heights:
        j = int(math.floor((centre2[1] + h - origin[1]) / ARCH_CELL))
        if not 0 <= j < n:
            continue
        edges = []
        for sign in (1.0, -1.0):
            f = centre2[0] + sign * reach
            i = np.floor((f - origin[0]) / ARCH_CELL).astype(int)
            ok = (i >= 0) & (i < n)
            v = np.full(len(reach), np.nan)
            v[ok] = depth[i[ok], j]
            found = step_edge(v)
            if found is None:
                break
            edges.append(f[found[0]])
        if len(edges) == 2:
            out.append((h, edges[0], edges[1]))
    return np.array(out).reshape(-1, 3)


def fit_arch(depth, origin, centre2, radius: float):
    """(centre (forward, up), radius, rms, edge points (arch_edge rows)) of the circle
    through the arch's visible edge (arch_edge on rays ARCH_BIN_DEG apart within ARCH_RAY_DEG
    of straight up, cast again from the fitted centre ARCH_ROUNDS times), or None with fewer
    than ARCH_MIN_LIPS edge points."""
    c = np.asarray(centre2, float)
    angles = np.arange(-ARCH_RAY_DEG, ARCH_RAY_DEG + 1e-9, ARCH_BIN_DEG)
    edge = None
    for _ in range(ARCH_ROUNDS):
        edge = arch_edge(depth, origin, c, radius, angles)
        if len(edge) < ARCH_MIN_LIPS:
            return None
        pts = edge[:, :2]
        a = np.c_[2 * pts, np.ones(len(pts))]
        sol = np.linalg.lstsq(a, (pts**2).sum(1), rcond=None)[0]
        c = sol[:2]
        rad = math.sqrt(max(sol[2] + c @ c, 0.0))
    res = np.hypot(*(edge[:, :2] - c).T) - rad
    return c, rad, float(np.sqrt((res**2).mean())), edge


def measure_wheel(body: np.ndarray, centre: np.ndarray, radius: float, width: float, outboard: float):
    """(fore_aft, arch_up, arch_radius, top_gap, lateral, rms, edge points) in metres for a tyre
    whose centre is `centre` (car frame: +x left, +y up, +z forward); outboard: +1 for a wheel
    on the +x side, -1 on the -x side."""
    extent = ARCH_REACH * radius + 0.05
    pts = wc.near_points(body, centre, extent, ARCH_CELL / 2)
    if len(pts) < 20:
        return None
    depth, origin = side_depth(pts, centre, outboard, extent, radius, abs(centre[0]) + width / 2)
    c0 = np.array([centre[2], centre[1]])
    top = arch_edge(depth, origin, c0, radius, [0.0])
    top_gap = float(top[0, 1] - c0[1] - radius) if len(top) else None
    sides = arch_sides(depth, origin, c0, radius, radius * np.arange(*ARCH_SIDE_HEIGHTS))
    fore_aft = float(np.median((sides[:, 1] + sides[:, 2]) / 2) - c0[0]) if len(sides) else None
    fan = arch_edge(depth, origin, c0, radius, np.arange(-ARCH_LIP_DEG, ARCH_LIP_DEG + 1e-9, ARCH_BIN_DEG))
    lateral = abs(centre[0]) + width / 2 - float(fan[:, 2].max()) if len(fan) else None
    fit = fit_arch(depth, origin, c0, radius)
    if fit is None:
        return fore_aft, None, None, top_gap, lateral, None, 0
    c, rad, rms, edge = fit
    return fore_aft, float(c[1] - c0[1]), rad, top_gap, lateral, rms, len(edge)


def measure(car: wc.Car, media: str = "") -> list[Fit]:
    """Every wheel of the car at rest in its arch."""
    fronts = [w.hub[2] for w in car.wheels if w.front]
    rears = [w.hub[2] for w in car.wheels if not w.front]
    rest = wc.rest_offsets(car.params, sum(fronts) / len(fronts), sum(rears) / len(rears))
    out = []
    for w in car.wheels:
        up = rest["front" if w.front else "rear"]
        centre = w.hub + np.array([0.0, up, 0.0]) + w.offset
        side = 1.0 if w.hub[0] > 0 else -1.0
        m = measure_wheel(car.body, centre, w.radius, w.width, side)
        mm = [None if v is None else v * 1000 for v in (m or (None,) * 6 + (0,))[:6]]
        fore, arch_up, _, top, lateral, rms = mm
        out.append(
            Fit(
                media,
                car.spool_id,
                w.name,
                w.radius,
                w.width,
                up * 1000,
                fore,
                arch_up,
                None if not m or m[2] is None else m[2],
                top,
                lateral,
                rms,
                m[6] if m else 0,
            )
        )
    return out


# --- CLI ------------------------------------------------------------------------------


def archive_ids(stream, entries) -> list[int]:
    """The spool IDs of the archive's vehicles that have a clean exterior entry (kind 2)."""
    from dsfforza.formats.vehicle_mesh import payload
    from dsfforza.formats.vehicles import find, parse_sslp

    ssic = find(entries, "SSIC")
    records = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
    ids = {sid for sid, _, _ in records}
    return sorted(sid for sid in ids if sid < 1000 and sid + 2000 in ids)


def _names(report: Path | None) -> dict[int, str]:
    if report is None:
        return {}
    data = json.loads(Path(report).read_text(encoding="utf-8"))
    return {c["spool_id"]: c["media"] for c in data.get("ok", [])}


def _rows(archive: Path, ids, names: dict[int, str], stream, entries):
    for sid in ids:
        try:
            car = wc.load_car(archive, sid, entries, stream)
            if len(car.wheels) != 4:
                continue
            yield from measure(car, names.get(sid, str(sid)))
        except Exception as error:  # noqa: BLE001 - printed; one broken car must not stop a survey
            print(f"{sid}: {error}", file=sys.stderr)


def cmd_car(args) -> None:
    from dsfforza.game.gui_assets import _root_entries

    with Path(args.archive).open("rb") as stream:
        entries = _root_entries(Path(args.archive), stream)
        print("\t".join(HEADER))
        for row in _rows(args.archive, args.ids, _names(args.report), stream, entries):
            print("\t".join(row.cells()))


def cmd_survey(args) -> None:
    from dsfforza.game.gui_assets import _root_entries

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    names = _names(args.report)
    with Path(args.archive).open("rb") as stream, out.open("w", encoding="utf-8", newline="\n") as f:
        entries = _root_entries(Path(args.archive), stream)
        ids = args.ids or (sorted(names) if names else archive_ids(stream, entries))
        f.write("\t".join(HEADER) + "\n")
        for n, row in enumerate(_rows(args.archive, ids, names, stream, entries)):
            f.write("\t".join(row.cells()) + "\n")
            if n % 100 == 0:
                f.flush()
                print(f"{row.media} {row.wheel}", flush=True)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="dsff wheel_fit", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("car", help="print one or more cars' wheels")
    a.add_argument("archive", type=Path)
    a.add_argument("ids", type=int, nargs="+")
    a.add_argument("--report", type=Path, help="batch-report.json, for the cars' names")
    a.set_defaults(func=cmd_car)
    a = sub.add_parser("survey", help="every four-wheeled vehicle of an archive into a TSV")
    a.add_argument("archive", type=Path)
    a.add_argument("--report", type=Path, help="batch-report.json: its cars and names (default: every vehicle)")
    a.add_argument("--ids", type=int, nargs="*")
    a.add_argument("--out", type=Path, required=True)
    a.set_defaults(func=cmd_survey)
    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
