"""How Reflections baked the sky term (s0.w) of vanilla vehicles (research/wheels.md, "Wheel sky
term: what the engine and vanilla's bake define", issue #525).

Reads the clean exterior entry (kind 2) of every four-wheeled vanilla vehicle and prints:

- `body`: per bin of the normal's up component, the median over the vehicles of each
  vehicle's 90th, 97th and 99th percentile of s0.w on the body (the envelope: the bake's
  value for an unoccluded vertex facing that way), next to (1 + n_y) / 2;
- `wheel`: per vehicle, s0.w of the wheel parts against `forza2dsf.wheel_sky`'s visibility
  (the wheel's own triangles as occluders, the inboard half-space closed) at an open level
  of 1: the least-squares scale k (the vehicle's open level) and the correlation r, for
  rays leaving past the tyre's radius counting 0 and 1.

Statistics only; no game data is written.

    uv run python tools/re/sky_bake.py body
    uv run python tools/re/sky_bake.py wheel [--ids 274 162] [--out out/re/sky_wheel.tsv]
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

import numpy as np

from dsfforza import REPO

ARCHIVE = REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"
EDGES = np.linspace(-1, 1, 21)
MIN_BIN = 30  # corners a bin needs for its percentiles
WHEEL_SAMPLE = 2000  # vertices per vehicle for the visibility march


def _unit(n):
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)


def _join(recs):
    return tuple(np.concatenate([r[k] for r in recs]) for k in range(3)) if recs else None


def load(archive: Path, sid: int, stream, entries):
    """Body and wheel-part corners of the clean exterior entry, each (pos, nrm, s0.w) or None.
    The blur disc (s9 byte 3 = 2, drawn only at speed) is left out."""
    from dsfforza.convert import forza2dsf
    from dsfforza.formats import upvh as uv
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload
    from dsfforza.game import car_render

    kinds = uv.entries_for(stream, entries, sid, None)
    full, ext = kinds[0], kinds[2]
    hw = forza2dsf.host_wheels(
        payload(stream, child(child(full, "NONR"), "UPVH")),
        payload(stream, child(child(full, "NONR"), "MODL")),
        payload(stream, child(child(ext, "NONR"), "MODL")),
    )
    _, mesh = vm.load(archive, entries.index(ext), entries)
    cache: dict = {}
    body, wheel = [], []
    for n, part in enumerate(mesh.parts):
        if not part.lods:
            continue
        lod = min((x for x in part.lods if x.mask > 1), key=lambda x: x.mask, default=part.lods[0])
        for i in lod.submodels:
            sub = mesh.submodels[i]
            pos, nrm, _, ao = car_render._submodel_triangles(mesh, sub, cache)
            if nrm is None or not len(pos) or car_render._fades_in(mesh, sub, cache):
                continue
            rec = (pos.reshape(-1, 3), _unit(nrm.reshape(-1, 3)), ao.reshape(-1))
            is_wheel = n in hw.exterior_parts or sub.material in car_render.TYRE_UIDS
            (wheel if is_wheel else body).append(rec)
    return _join(body), _join(wheel)


def vehicles(archive: Path, ids):
    from dsfforza.convert.wheel_fit import archive_ids
    from dsfforza.game.gui_assets import _root_entries

    with archive.open("rb") as stream:
        entries = _root_entries(archive, stream)
        for sid in ids or archive_ids(stream, entries):
            try:
                body, wheel = load(archive, sid, stream, entries)
            except (ValueError, KeyError, StopIteration, IndexError) as error:
                print(f"{sid}: {error}", file=sys.stderr)
                continue
            yield sid, body, wheel


def cmd_body(args) -> None:
    per = []
    for _, body, _ in vehicles(args.archive, args.ids):
        if body is None:
            continue
        _, nrm, ao = body
        row = []
        for lo, hi in itertools.pairwise(EDGES):
            sel = (nrm[:, 1] >= lo) & (nrm[:, 1] < hi)
            row.append(np.percentile(ao[sel], [90, 97, 99]) if sel.sum() >= MIN_BIN else [np.nan] * 3)
        per.append(row)
    p = np.array(per)
    print(f"{len(per)} vehicles; s0.w envelope of the body by n_y (median over vehicles)")
    print("n_y    (1+n_y)/2   p90    p97    p99")
    for i, (lo, hi) in enumerate(itertools.pairwise(EDGES)):
        m = (lo + hi) / 2
        q = np.nanmedian(p[:, i, :], 0)
        print(f"{m:+.2f}  {(1 + m) / 2:.3f}      {q[0]:.3f}  {q[1]:.3f}  {q[2]:.3f}")


def wheel_fit(wheel, sides=(0.0, 1.0)) -> dict[float, tuple[float, float]]:
    """{side share: (k, r)}: s0.w of the wheel against wheel_sky's visibility at open level 1."""
    from dsfforza.convert.forza2dsf import wheels as w

    pos, nrm, ao = wheel
    _, idx = np.unique(np.round(np.concatenate([pos, nrm], 1), 4), axis=0, return_index=True)
    if len(idx) > WHEEL_SAMPLE:
        idx = np.random.default_rng(0).choice(idx, WHEEL_SAMPLE, replace=False)
    pts, nn, a = pos[idx], nrm[idx], ao[idx]
    saved = w.WHEEL_SKY_OPEN, w.WHEEL_SKY_SIDE
    out = {}
    try:
        for side in sides:
            w.WHEEL_SKY_OPEN, w.WHEEL_SKY_SIDE = 1.0, side
            v = np.array(w.wheel_sky(pts, nn, pos.reshape(-1, 3, 3)))
            out[side] = (float(v @ a / (v @ v)), float(np.corrcoef(v, a)[0, 1]))
    finally:
        w.WHEEL_SKY_OPEN, w.WHEEL_SKY_SIDE = saved
    return out


def cmd_wheel(args) -> None:
    rows = []
    for sid, _, wheel in vehicles(args.archive, args.ids):
        if wheel is None:
            continue
        fit = wheel_fit(wheel)
        rows.append((sid, float(wheel[2].max()), *fit[0.0], *fit[1.0]))
        print(
            f"{sid}\tmax {rows[-1][1]:.3f}\tside 0: k {fit[0.0][0]:.3f} r {fit[0.0][1]:.3f}"
            f"\tside 1: k {fit[1.0][0]:.3f} r {fit[1.0][1]:.3f}",
            flush=True,
        )
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        lines = ["sid\tmax\tk_side0\tr_side0\tk_side1\tr_side1"]
        lines += ["\t".join([str(r[0]), *(f"{x:.4f}" for x in r[1:])]) for r in rows]
        args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t = np.array([r[1:] for r in rows])
    for i, name in enumerate(["max", "k side 0", "r side 0", "k side 1", "r side 1"]):
        q = np.nanpercentile(t[:, i], [5, 25, 50, 75, 95])
        print(f"{name:9s} n {len(t)}  p5 {q[0]:.3f}  p25 {q[1]:.3f}  median {q[2]:.3f}  p75 {q[3]:.3f}  p95 {q[4]:.3f}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("what", choices=("body", "wheel"))
    ap.add_argument("--archive", type=Path, default=ARCHIVE)
    ap.add_argument("--ids", type=int, nargs="*")
    ap.add_argument("--out", type=Path)
    a = ap.parse_args(argv)
    (cmd_body if a.what == "body" else cmd_wheel)(a)


if __name__ == "__main__":
    main()
