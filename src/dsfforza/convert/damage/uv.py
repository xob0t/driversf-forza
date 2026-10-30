"""Decal and scratch UVs (UV1): the host's UV1 field and nearest-triangle lookups on it."""

from __future__ import annotations

import math
from collections import defaultdict

import numpy as np

from dsfforza.convert.damage.core import (
    FACING_WEIGHT,
    Box,
    Grid,
    _angles_of,
    _cross,
    _dot,
    _norm,
    _normal_of,
    _sub,
    load_mesh,
)
from dsfforza.formats import vehicle_mesh as vm
from dsfforza.formats.vehicle_mesh import child, payload

# --- decal / scratch UVs ------------------------------------------------------------
#
# The vehicle pixel shader samples the shared damage-decal texture (SSIC
# 00122b6db3fa1227, 1024x1024 DXT5: dent normals in RGB, scratch mask in
# alpha) and the dirt texture (001251560b05e528) at UV1, semantic 8
# components 2-3 of the 32-byte buffers (paint and lamps). Vanilla cars unwrap
# each panel onto its own tile of that atlas (front bumper top left, rear
# bumper top right, bonnet, boot, sides in the rows below; lamps on the
# small broken-lamp tile). Forza's own UV1 (paint) or the lamp atlas UV would show
# scratch tiles in the wrong places as bright white shapes. Uv1Field projects each new
# vertex onto the nearest host triangle of the same panel and material and
# interpolates the host's UV1 there.


def closest_on_triangle(p, a, b, c):
    """Closest point to p on triangle abc: (squared distance, barycentric (u, v, w))."""
    ab, ac, ap = _sub(b, a), _sub(c, a), _sub(p, a)
    d1, d2 = _dot(ab, ap), _dot(ac, ap)
    if d1 <= 0 and d2 <= 0:
        bary = (1.0, 0.0, 0.0)
    else:
        bp = _sub(p, b)
        d3, d4 = _dot(ab, bp), _dot(ac, bp)
        cp = _sub(p, c)
        d5, d6 = _dot(ab, cp), _dot(ac, cp)
        vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
        if d3 >= 0 and d4 <= d3:
            bary = (0.0, 1.0, 0.0)
        elif d6 >= 0 and d5 <= d6:
            bary = (0.0, 0.0, 1.0)
        elif vc <= 0 and d1 >= 0 and d3 <= 0:
            t = d1 / (d1 - d3) if d1 != d3 else 0.0
            bary = (1 - t, t, 0.0)
        elif vb <= 0 and d2 >= 0 and d6 <= 0:
            t = d2 / (d2 - d6) if d2 != d6 else 0.0
            bary = (1 - t, 0.0, t)
        elif va <= 0 and (d4 - d3) >= 0 and (d5 - d6) >= 0:
            t = (d4 - d3) / ((d4 - d3) + (d5 - d6)) if (d4 - d3) + (d5 - d6) else 0.0
            bary = (0.0, 1 - t, t)
        else:
            denom = va + vb + vc
            v, w = (vb / denom, vc / denom) if denom else (0.0, 0.0)
            bary = (1 - v - w, v, w)
    q = tuple(bary[0] * a[k] + bary[1] * b[k] + bary[2] * c[k] for k in range(3))
    d = _sub(p, q)
    return _dot(d, d), bary


def closest_on_triangles(p, corners):
    """closest_on_triangle for many triangles at once (corners: (n, 3, 3) array of
    a, b, c), with its float operations and branch order: (squared distances,
    barycentric (n, 3))."""
    corners = np.asarray(corners, dtype=np.float64)
    d, b0, b1, b2 = _closest(p, *(np.ascontiguousarray(corners[:, i // 3, i % 3]) for i in range(9)))
    return d, np.stack((b0, b1, b2), axis=1)


def _closest(p, ax, ay, az, bx, by, bz, cx, cy, cz):
    """closest_on_triangle over component arrays: (squared distance, barycentric u, v, w)."""
    px, py, pz = p
    abx, aby, abz, acx, acy, acz = bx - ax, by - ay, bz - az, cx - ax, cy - ay, cz - az
    apx, apy, apz = px - ax, py - ay, pz - az
    d1 = abx * apx + aby * apy + abz * apz
    d2 = acx * apx + acy * apy + acz * apz
    bpx, bpy, bpz = px - bx, py - by, pz - bz
    d3 = abx * bpx + aby * bpy + abz * bpz
    d4 = acx * bpx + acy * bpy + acz * bpz
    cpx, cpy, cpz = px - cx, py - cy, pz - cz
    d5 = abx * cpx + aby * cpy + abz * cpz
    d6 = acx * cpx + acy * cpy + acz * cpz
    vc, vb, va = d1 * d4 - d3 * d2, d5 * d2 - d1 * d6, d3 * d6 - d5 * d4
    e, f = d4 - d3, d5 - d6
    denom = va + vb + vc
    t3 = np.divide(d1, d1 - d3, out=np.zeros_like(d1), where=d1 != d3)
    t4 = np.divide(d2, d2 - d6, out=np.zeros_like(d1), where=d2 != d6)
    t5 = np.divide(e, e + f, out=np.zeros_like(d1), where=(e + f) != 0)
    v = np.divide(vb, denom, out=np.zeros_like(d1), where=denom != 0)
    w = np.divide(vc, denom, out=np.zeros_like(d1), where=denom != 0)
    # The branches from the last to the first, so the first that applies wins.
    b0, b1, b2 = 1 - v - w, v, w
    for cond, (x0, x1, x2) in (
        ((va <= 0) & (e >= 0) & (f >= 0), (0.0, 1 - t5, t5)),
        ((vb <= 0) & (d2 >= 0) & (d6 <= 0), (1 - t4, 0.0, t4)),
        ((vc <= 0) & (d1 >= 0) & (d3 <= 0), (1 - t3, t3, 0.0)),
        ((d6 >= 0) & (d5 <= d6), (0.0, 0.0, 1.0)),
        ((d3 >= 0) & (d4 <= d3), (0.0, 1.0, 0.0)),
        ((d1 <= 0) & (d2 <= 0), (1.0, 0.0, 0.0)),
    ):
        if cond.any():
            b0, b1, b2 = np.where(cond, x0, b0), np.where(cond, x1, b1), np.where(cond, x2, b2)
    dx = px - (b0 * ax + b1 * bx + b2 * cx)
    dy = py - (b0 * ay + b1 * by + b2 * cy)
    dz = pz - (b0 * az + b1 * bz + b2 * cz)
    return dx * dx + dy * dy + dz * dz, b0, b1, b2


def _tri_arrays(f) -> tuple:
    """(corner components ax, ay, az, bx, ..., cz, normal components nx, ny, nz) of a
    HostField's triangles as arrays, made once."""
    cached = f.__dict__.get("_tri_arrays")
    if cached is None or cached[0] != len(f.tris):
        corners = np.array([t[0] for t in f.tris], dtype=np.float64).reshape(-1, 3, 3)
        normals = np.array([t[2] for t in f.tris], dtype=np.float64).reshape(-1, 3)
        cached = f._tri_arrays = (
            len(f.tris),
            tuple(np.ascontiguousarray(corners[:, i // 3, i % 3]) for i in range(9)),
            tuple(np.ascontiguousarray(normals[:, i]) for i in range(3)),
        )
    return cached[1], cached[2]


class Uv1Field:
    """The host's UV1 (decal-atlas) triangles of one entry's MODL part 0, in
    coordinates normalised by the host car box, grouped for lookups by
    (panel pair, material), material class and all."""

    def __init__(self, tris, box: Box):
        self.box = box
        self.tris = tris  # (normalised a, b, c), (uv a, b, c), pair, material, local, unit normal
        self.groups: dict[tuple, Grid] = {}
        keys = defaultdict(list)
        for n, t in enumerate(tris):
            uvs = t[1]
            # Triangles whose UV1 collapses to one texel are the surfaces
            # vanilla never scratches: hidden faces, and on the outside everything above
            # the waist (pillars, window frames, roof; host 239: all paint above 1.0 m),
            # all on one clean texel in a corner of the atlas. They are candidates like
            # the rest (kept back for a last resort, the X6 M's upper doors and roof would
            # take the nearest scratch tile, clamped at its edge into long streaks).
            # _split_seams keeps a triangle from spanning the clean texel and a tile.
            if any(not -0.01 <= uv[k] <= 1.01 for uv in uvs for k in (0, 1)):
                # Tiled UV1 (vanilla interiors' GLOBAL_LOD_Chrome runs to -4..5): not the
                # panel atlas layout.
                keys[("tiled",)].append(n)
                continue
            keys[("pm", t[2], t[3])].append(n)
            keys[("pc", t[2], t[4])].append(n)
            keys[("c", t[4])].append(n)
            keys[("all",)].append(n)
        # Charts: triangles joined through vertices with the same position and UV1.
        parent = list(range(len(tris)))

        def find(n):
            while parent[n] != n:
                parent[n] = parent[parent[n]]
                n = parent[n]
            return n

        owner = {}
        for n, t in enumerate(tris):
            for p, uv in zip(t[0], t[1]):
                key = (tuple(round(c, 4) for c in p), round(uv[0], 4), round(uv[1], 4))
                if key in owner:
                    parent[find(n)] = find(owner[key])
                else:
                    owner[key] = n
        self.chart = [find(n) for n in range(len(tris))]
        for n in keys[("all",)]:
            keys[("chart", self.chart[n])].append(n)
        for key, ns in keys.items():
            self.groups[key] = Grid(
                [tuple(sum(v[a] for v in tris[n][0]) / 3 for a in range(3)) for n in ns], ns, cell=0.06
            )

    @staticmethod
    def read(stream, vehc, mask: int, local_materials: set) -> Uv1Field | None:
        mesh = load_mesh(stream, vehc)
        raw = []
        for lod in mesh.parts[0].lods:
            if lod.mask != mask:
                continue
            for n in lod.submodels:
                sub = mesh.submodels[n]
                for vb, tri in vm.triangles(mesh, sub):
                    e8 = vb.element(8)
                    if e8 is None or e8.components != 4:
                        continue
                    pts = [vm.position(vb, i) for i in tri]
                    uvs = [tuple(vb.read(e8, i)[2:4]) for i in tri]
                    normal = _norm(_cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0])))
                    if normal is None:
                        continue
                    raw.append((pts, uvs, s9_pair(vb, tri[0]), sub.material, sub.material in local_materials, normal))
        if not raw:
            return None
        box = Box.of(p for t in raw for p in t[0])
        c, s = box.centre, box.size

        def norm(p):
            return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))

        return Uv1Field([(tuple(norm(p) for p in t[0]),) + t[1:] for t in raw], box)

    def lookup(
        self, q, pair, material: bytes, local: bool, any_panel: bool = False, normal=None, chart: int | None = None
    ):
        """(UV1, chart) at normalised point q from the nearest host triangle
        facing the same way (`normal`, car space): same panel and material, else
        same panel and material class, else same class; only `chart` if given."""
        for key in self._keys(pair, material, local, any_panel, chart):
            grid = self.groups.get(key)
            if grid is None:
                continue
            best = None
            for _, n in grid.nearest(q, 16):
                t = self.tris[grid.values[n]]
                d, bary = closest_on_triangle(q, *t[0])
                score = math.sqrt(d) + (FACING_WEIGHT * (1 - _dot(normal, t[5])) if normal else 0.0)
                if best is None or score < best[0]:
                    best = (score, bary, t[1], self.chart[grid.values[n]])
            if best is not None:
                bary, uvs = best[1], best[2]
                return (
                    (sum(bary[k] * uvs[k][0] for k in range(3)), sum(bary[k] * uvs[k][1] for k in range(3))),
                    best[3],
                )
        return None

    @staticmethod
    def _keys(pair, material: bytes, local: bool, any_panel: bool, chart: int | None) -> list:
        """The groups lookup tries, in order."""
        return (
            ([("chart", chart)] if chart is not None else [])
            + ([] if any_panel else [("pm", pair, material), ("pc", pair, local)])
            + [("c", local), ("all",), ("tiled",)]
        )

    def lookup_many(self, items) -> list:
        """lookup for many (q, pair, material, local, any_panel, normal, chart) at
        once, with the same result for each. lookup's result comes from the first
        group it has that has points: the candidates are that group's 16 nearest
        (Grid.nearest_many), each scored as lookup does (_closest, the same floats
        as closest_on_triangle), and the first lowest score in nearest's order wins
        (lookup keeps a best only on a strictly lower score). The winner's
        barycentric coordinates and UV1 are lookup's own code. An item with a
        non-finite score goes to lookup."""
        out: list = [None] * len(items)
        by_group = defaultdict(list)
        for i, (_, pair, material, local, any_panel, _, chart) in enumerate(items):
            for key in self._keys(pair, material, local, any_panel, chart):
                grid = self.groups.get(key)
                if grid is not None and grid.points:
                    by_group[key].append(i)
                    break
        if not by_group:
            return out
        if self.__dict__.get("_arrays") is None:
            corners = np.array([t[0] for t in self.tris], dtype=np.float64).reshape(-1, 3, 3)
            normals = np.array([t[5] for t in self.tris], dtype=np.float64).reshape(-1, 3)
            self._arrays = (
                tuple(np.ascontiguousarray(corners[:, i // 3, i % 3]) for i in range(9)),
                tuple(np.ascontiguousarray(normals[:, i]) for i in range(3)),
            )
        corners, tnormals = self._arrays
        slow = []
        for key, idx in by_group.items():
            grid = self.groups[key]
            hits = grid.nearest_many([items[i][0] for i in idx], 16)
            values = grid.values
            tri = np.array([values[n] for h in hits for _, n in h], dtype=np.int64)
            lens = np.array([len(h) for h in hits], dtype=np.int64)
            g = np.repeat(np.arange(len(idx)), lens)
            q = np.array([items[i][0] for i in idx], dtype=np.float64).reshape(-1, 3)[g]
            normals = [items[i][5] for i in idx]
            has = np.array([bool(n) for n in normals])[g]
            nrm = np.array([n if n else (0.0, 0.0, 0.0) for n in normals], dtype=np.float64).reshape(-1, 3)[g]
            d = _closest((q[:, 0], q[:, 1], q[:, 2]), *(c[tri] for c in corners))[0]
            dot = nrm[:, 0] * tnormals[0][tri] + nrm[:, 1] * tnormals[1][tri] + nrm[:, 2] * tnormals[2][tri]
            score = np.sqrt(d) + np.where(has, FACING_WEIGHT * (1 - dot), 0.0)
            first = np.cumsum(lens) - lens
            bad = np.bincount(g, weights=(~np.isfinite(score)).astype(np.float64), minlength=len(idx)) > 0
            low = np.minimum.reduceat(np.where(np.isfinite(score), score, 0.0), first)
            at = np.flatnonzero(score == low[g])
            _, firsts = np.unique(g[at], return_index=True)
            win = dict(zip(g[at[firsts]].tolist(), tri[at[firsts]].tolist(), strict=True))
            for m, i in enumerate(idx):
                if bad[m]:
                    slow.append(i)
                    continue
                n = win[m]
                t = self.tris[n]
                _, bary = closest_on_triangle(items[i][0], *t[0])
                uvs = t[1]
                out[i] = (
                    (sum(bary[k] * uvs[k][0] for k in range(3)), sum(bary[k] * uvs[k][1] for k in range(3))),
                    self.chart[n],
                )
        for i in slow:
            out[i] = self.lookup(*items[i])
        return out


def s9_pair(vb, index: int) -> tuple[int, int]:
    """Semantic 9 bytes 0, 1 (panel transform, panel data) of a vertex; (0, 0),
    the chassis, for buffers without semantic 9 (some vanilla interiors, e.g.
    173 and 184, have 32-byte buffers without it)."""
    e9 = vb.element(9)
    if e9 is None:
        return (0, 0)
    s9 = vb.read(e9, index)
    return (s9[0], s9[1])


def stored_normal(buf, at: int):
    """Unit normal from semantic 5 bytes 0, 1 at `at` (as the shader decodes them)."""
    a0, a1 = _angles_of(buf[at], buf[at + 1])
    return _normal_of(a0, a1)


def local_materials(stream, vehc) -> set:
    """Material UIDs an entry's own DRSP defines (the rest are SSIC shared ones)."""
    return {
        payload(stream, child(e, "MAHE"))[:8] for e in child(child(vehc, "MDXN"), "DRSP").children if e.tag == "MACO"
    }
