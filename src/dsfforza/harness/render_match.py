"""Which Forza triangle each built triangle came from (render_compare's ID buffer).

The converter writes every body vertex at `round(Placement.point(p) x 4096)`
(forza2dsf.encode_vertex), so a built triangle lies on the Forza triangle it was made
from: on it exactly for the triangles add_geometry copies, inside it for the pieces
the converter cuts out of one (cutout's badges, damage.split_exposed). The match is
the Forza triangle every corner of the built one lies on: per built triangle, the
candidate (a reference triangle whose box, widened by `NEAR`, holds the centroid) with
the smallest largest corner distance (`corner_distance`). Up to `ON` that is the
triangle it came from; up to `NEAR` a near match (decimated cockpits, plates and
lenses moved off their face); farther, none.

Works on any build: it needs only the built entry and the Forza carbin at the same
LOD, and the placement (the build's per-car report, else `register`).
"""

from __future__ import annotations

import numpy as np

ON = 0.0015  # m: corners this close to a reference triangle lie on it (positions are 1/4096 m)
NEAR = 0.02  # m: a near match
CELL = 0.04  # m: grid cell of the candidate search
MAX_CELLS = 4096  # cells one reference triangle is entered in (larger ones are skipped)


def point_triangle_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    """Distance (n,) from points p (n, 3) to triangles abc (n, 3 each): the closest
    point by Voronoi region (Ericson, Real-Time Collision Detection 5.1.5)."""
    ab, ac, ap = b - a, c - a, p - a
    d1 = np.einsum("ni,ni->n", ab, ap)
    d2 = np.einsum("ni,ni->n", ac, ap)
    bp = p - b
    d3 = np.einsum("ni,ni->n", ab, bp)
    d4 = np.einsum("ni,ni->n", ac, bp)
    cp = p - c
    d5 = np.einsum("ni,ni->n", ab, cp)
    d6 = np.einsum("ni,ni->n", ac, cp)
    va = d3 * d6 - d5 * d4
    vb = d5 * d2 - d1 * d6
    vc = d1 * d4 - d3 * d2
    eps = 1e-30
    # Face region by default.
    denom = va + vb + vc
    v = vb / np.where(np.abs(denom) > eps, denom, eps)
    w = vc / np.where(np.abs(denom) > eps, denom, eps)
    q = a + ab * v[:, None] + ac * w[:, None]
    # Edge BC
    cond = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
    t = (d4 - d3) / np.where(np.abs((d4 - d3) + (d5 - d6)) > eps, (d4 - d3) + (d5 - d6), eps)
    q = np.where(cond[:, None], b + (c - b) * t[:, None], q)
    # Edge AC
    cond = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
    t = d2 / np.where(np.abs(d2 - d6) > eps, d2 - d6, eps)
    q = np.where(cond[:, None], a + ac * t[:, None], q)
    # Edge AB
    cond = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
    t = d1 / np.where(np.abs(d1 - d3) > eps, d1 - d3, eps)
    q = np.where(cond[:, None], a + ab * t[:, None], q)
    # Vertices
    q = np.where(((d6 >= 0) & (d5 <= d6))[:, None], c, q)
    q = np.where(((d3 >= 0) & (d4 <= d3))[:, None], b, q)
    q = np.where(((d1 <= 0) & (d2 <= 0))[:, None], a, q)
    return np.linalg.norm(p - q, axis=1)


def _cell_key(c: np.ndarray) -> np.ndarray:
    c = c.astype(np.int64) + (1 << 20)
    return (c[:, 0] << 42) | (c[:, 1] << 21) | c[:, 2]


def _reference_cells(ref: np.ndarray, cell: float, pad: float):
    """(sorted cell keys, triangle per key) of every cell a reference triangle's box,
    widened by `pad`, overlaps."""
    lo = np.floor((ref.min(axis=1) - pad) / cell).astype(np.int64)
    hi = np.floor((ref.max(axis=1) + pad) / cell).astype(np.int64)
    ext = hi - lo + 1
    count = ext.prod(axis=1)
    keep = np.flatnonzero(count <= MAX_CELLS)
    tri = np.repeat(keep, count[keep])
    local = np.arange(len(tri)) - np.repeat(np.cumsum(count[keep]) - count[keep], count[keep])
    e = ext[tri]
    ix = local % e[:, 0]
    iy = (local // e[:, 0]) % e[:, 1]
    iz = local // (e[:, 0] * e[:, 1])
    keys = _cell_key(lo[tri] + np.stack((ix, iy, iz), axis=1))
    order = np.argsort(keys, kind="stable")
    return keys[order], tri[order], np.flatnonzero(count > MAX_CELLS)


def _expand(left: np.ndarray, right: np.ndarray, values: np.ndarray, rows: np.ndarray):
    """(row, value) pairs for every row's range values[left:right]."""
    count = right - left
    row = np.repeat(rows, count)
    at = np.repeat(left, count) + np.arange(len(row)) - np.repeat(np.cumsum(count) - count, count)
    return row, values[at]


def _best(ours, centre, ref, qi, ci, src, best, chunk: int) -> None:
    """Keep, per query, the candidate with the smallest largest corner distance (ties
    on the shared edge of two coplanar triangles: the centroid decides)."""
    for s in range(0, len(qi), chunk):
        q, c = qi[s : s + chunk], ci[s : s + chunk]
        a, b, cc = ref[c, 0], ref[c, 1], ref[c, 2]
        d = np.maximum.reduce([point_triangle_distance(ours[q, k], a, b, cc) for k in range(3)])
        score = d + 0.01 * point_triangle_distance(centre[q], a, b, cc)
        order = np.lexsort((score, q))
        q, c, score = q[order], c[order], score[order]
        first = np.r_[True, q[1:] != q[:-1]]
        q, c, score = q[first], c[first], score[first]
        better = score < best[q]
        best[q[better]] = score[better]
        src[q[better]] = c[better]


EXACT_CELL = 0.001  # m: first pass, triangles copied as they were (centroids within a unit or two)


def match(ours: np.ndarray, ref: np.ndarray, chunk: int = 1 << 18) -> tuple[np.ndarray, np.ndarray]:
    """For built triangles ours (N, 3, 3) and reference triangles ref (M, 3, 3), both in
    DSF car space: (source (N,): the reference triangle, -1 for none; distance (N,):
    the largest corner distance to it, m). A first pass looks for the copied triangles
    among the reference triangles whose centroid is in the neighbouring millimetre cells,
    the second for the rest among the triangles whose widened box holds the centroid."""
    n = len(ours)
    src = np.full(n, -1, np.int64)
    best = np.full(n, np.inf)
    if not n or not len(ref):
        return src, np.full(n, np.inf)
    ours = ours.astype(np.float64)
    ref = ref.astype(np.float64)
    centre = ours.mean(axis=1)
    # Pass 1: copied triangles.
    rc = np.floor(ref.mean(axis=1) / EXACT_CELL).astype(np.int64)
    rkeys = _cell_key(rc)
    order = np.argsort(rkeys, kind="stable")
    rkeys, rtri = rkeys[order], order
    qc = np.floor(centre / EXACT_CELL).astype(np.int64)
    rows = np.arange(n)
    for off in np.stack(np.meshgrid([-1, 0, 1], [-1, 0, 1], [-1, 0, 1]), -1).reshape(-1, 3):
        q = _cell_key(qc + off)
        qi, ci = _expand(np.searchsorted(rkeys, q, "left"), np.searchsorted(rkeys, q, "right"), rtri, rows)
        _best(ours, centre, ref, qi, ci, src, best, chunk)
    # Pass 2: what is not on a copied triangle.
    rest = np.flatnonzero(best > ON)
    if len(rest):
        keys, tris, big = _reference_cells(ref, CELL, NEAR)
        q = _cell_key(np.floor(centre[rest] / CELL).astype(np.int64))
        qi, ci = _expand(np.searchsorted(keys, q, "left"), np.searchsorted(keys, q, "right"), tris, rest)
        if len(big):  # huge reference triangles (a few, if any): every query is their candidate
            qi = np.concatenate([qi, np.repeat(rest, len(big))])
            ci = np.concatenate([ci, np.tile(big, len(rest))])
        _best(ours, centre, ref, qi, ci, src, best, chunk)
    dist = np.full(n, np.inf)
    on = src >= 0
    if on.any():
        a, b, c = ref[src[on], 0], ref[src[on], 1], ref[src[on], 2]
        dist[on] = np.maximum.reduce([point_triangle_distance(ours[on, k], a, b, c) for k in range(3)])
    src[dist > NEAR] = -1
    return src, dist


def register(ours_vertices: np.ndarray, ref_vertices: np.ndarray, guess=(0.0, 0.0), samples: int = 4000) -> tuple:
    """(dy, dz) that moves reference vertices (Placement with dy = dz = 0) onto built
    ones, for a build without per-car reports: the most common offset between built
    vertices and reference vertices at the same x (x is only mirrored by the placement),
    in 1 mm bins, then the median of the offsets within 2 mm of it."""
    rng = np.random.default_rng(0)
    o = ours_vertices[rng.choice(len(ours_vertices), min(samples, len(ours_vertices)), replace=False)]
    r = ref_vertices[np.argsort(ref_vertices[:, 0])]
    lo = np.searchsorted(r[:, 0], o[:, 0] - 2.5e-4)
    hi = np.searchsorted(r[:, 0], o[:, 0] + 2.5e-4)
    count = np.minimum(hi - lo, 64)
    qi = np.repeat(np.arange(len(o)), count)
    ri = np.repeat(lo, count) + np.arange(len(qi)) - np.repeat(np.cumsum(count) - count, count)
    if not len(qi):
        return tuple(guess)
    d = o[qi, 1:] - r[ri, 1:]
    bins = np.round(d / 1e-3).astype(np.int64)
    uniq, counts = np.unique(bins, axis=0, return_counts=True)
    peak = uniq[np.argmax(counts)] * 1e-3
    near = np.all(np.abs(d - peak) < 2e-3, axis=1)
    dy, dz = np.median(d[near], axis=0) if near.any() else peak
    return float(dy), float(dz)
