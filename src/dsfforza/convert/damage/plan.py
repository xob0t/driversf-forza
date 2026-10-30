"""DamagePlan: one car's damage model on its host (panels, crush fields, decal UVs, exposed
geometry, hierarchy)."""

from __future__ import annotations

import itertools
import math
import struct
from collections import Counter, defaultdict

import numpy as np

from dsfforza.convert.damage.core import (
    _SHELLS,
    BODY,
    COLLISION_SCALE,
    DOORS,
    LAYOUT,
    MAX_HINGE_MOVE,
    MIRRORS,
    MOUNT_REACH,
    MOUNT_TOUCH,
    NOT_MOVED,
    PAINT_REACH,
    PANEL_FACING,
    POSITION_UNITS,
    SEAM_BORDER,
    SEAM_NEAR,
    SEAM_REACH,
    SEAM_STILL,
    SIDE_PARTNER,
    Box,
    Grid,
    _cross,
    _dot,
    _norm,
    _sub,
    normal_delta,
    role_name,
)
from dsfforza.convert.damage.exposed import (
    BAY_DEPTH,
    BOOT_DEPTH,
    EXPOSED_MAP,
    EXPOSED_SENTINEL,
    _kind_of,
    exposed_sky,
    exposed_zones,
    smooth_labels,
)
from dsfforza.convert.damage.host import HostField, Panel, host_panels, mirror_key, section_rule
from dsfforza.convert.damage.host_cache import DamageOptions, host_setup
from dsfforza.convert.damage.parts import (
    PART_MIN_AREA,
    PART_SHARE,
    SWINGS,
    in_door_card,
    part_components,
    part_relabel,
    part_spans,
    part_stats,
    part_trim,
    tear_length,
    within_span,
)
from dsfforza.convert.damage.tree import (
    _collision_bottom,
    _floor_collision,
    _map_collision,
    _move_in_turn,
    _parents,
    _scale_collision,
    compact,
    robust_box,
    trimmed_box,
)
from dsfforza.convert.damage.uv import _closest, _tri_arrays, closest_on_triangle, local_materials, stored_normal
from dsfforza.formats import upvh as upvh_mod
from dsfforza.formats import vehicle_mesh as vm
from dsfforza.formats.vehicle_mesh import child, payload

VOTE_RINGS = (1, 2, 3, 5, 7)  # DamagePlan._votes: rings (at most 7) up to which points are decided in turn
VOTE_BLOCK = 4  # DamagePlan._votes: candidates are found per block of this many cells a side
VOTE_CHUNK = 400_000  # DamagePlan._votes: candidates scored at once


class DamagePlan:
    """Everything the damage conversion needs from the host, read before the
    converter replaces the host's geometry."""

    def __init__(
        self,
        stream,
        full,
        exterior,
        car,
        place,
        opts: DamageOptions | None = None,
        log=print,
        interior=None,
        engine: str | None = None,
    ):
        from dsfforza.convert import forza2dsf

        self.f2d = forza2dsf
        self.opts = opts or DamageOptions()
        self.place = place
        self.car = car
        self.log = log
        nonr = child(full, "NONR")
        self.upvh = payload(stream, child(nonr, "UPVH"))
        self.panels = host_panels(self.upvh, payload(stream, child(nonr, "MODL")))
        self.full_field, self.ext_field, self.int_uv1 = host_setup(stream, full, exterior, interior)
        self.chassis = (0, 0) if (0, 0) in self.panels else min(self.panels)
        if interior is not None:
            self.int_local = local_materials(stream, interior)
        # The Forza car's box (placed), over its finest body LOD.
        lods = sorted({sub.lod for s in car.body for sub in s.subsections if sub.lod > 0})
        pts = [
            place.point(v.position)
            for s in car.body
            for sub in s.subsections
            if sub.lod == lods[0]
            for t in sub.triangles()
            for v in (s.vertices_for(sub)[i] for i in t)
        ]
        self.forza_box = Box.of(pts)
        self.forza_hull_box = trimmed_box(set(pts))
        # Projection boxes for exposed inner geometry (split_exposed, _expose).
        self.exposed_boxes: dict[str, Box] = {}
        for name in ("hood", "trunk"):
            box = Box.of(
                place.point(v.position)
                for s in car.body
                if s.name.lower().startswith(name)
                for sub in s.subsections
                if sub.lod == lods[0]
                for t in sub.triangles()
                for v in (s.vertices_for(sub)[i] for i in t)
            )
            if box is not None:
                self.exposed_boxes[name] = box
        # Zones (exposed_zones): under a cover, its box deepened by BAY_DEPTH or
        # BOOT_DEPTH; behind a bumper, its box deepened inward by BUMPER_DEPTH.
        for kind, is_panel, axis, sign, _, reach in exposed_zones(engine):
            box = Box.of(
                place.point(v.position)
                for s in car.body
                if is_panel(s.name.lower())
                for sub in s.subsections
                if sub.lod == lods[0]
                for t in sub.triangles()
                for v in (s.vertices_for(sub)[i] for i in t)
            )
            if box is None:
                continue
            lo, hi = list(box.lo), list(box.hi)
            if kind in ("bay", "boot"):
                lo[1] -= BAY_DEPTH if kind == "bay" else BOOT_DEPTH
            elif sign > 0:
                hi[axis] = lo[axis] + reach
            else:
                lo[axis] = hi[axis] - reach
            self.exposed_boxes[kind] = Box(tuple(lo), tuple(hi))
        self._assign: dict[tuple, dict] = {}
        self._members: dict[tuple, dict] = {}
        self._grids: dict[tuple, Grid] = {}
        self.boxes: dict[tuple, list] = defaultdict(list)  # pair -> placed points (finest LOD built)
        self.drawn: set = set()  # pairs given geometry in any LOD built so far
        self._boxes_lod: int | None = None
        self._seam: dict = {}  # quantised position -> rigid panels sharing it (current LOD)
        self._seam_written: set = set()  # (field, position) given a shared crushed position
        self._seam_rep: dict = {}  # quantised position -> its near-coincident group's position
        self._seam_paint: set = set()  # quantised positions on body paint (the outer skin)
        self._pins: dict = {}  # pair -> Grid of its pinned seam positions (current LOD)
        self._motion: dict = {}  # id(HostField) -> {frozenset of roles: median host border crush}
        self.swinging: set = set()  # doors/wings that are Forza parts (assignment): own transform slot
        self.report: dict = {"panels": {}}

    # -- panel lookup --
    def panels_with(self, roles) -> list[Panel]:
        return [p for p in self.panels.values() if p.role in roles]

    def _partner(self, pair) -> tuple | None:
        """The other side's panel of a sided role (mirror +x for mirror -x)."""
        p = self.panels.get(pair)
        role = SIDE_PARTNER.get(p.role) if p is not None else None
        cands = sorted(q.pair for q in self.panels_with((role,))) if role is not None else []
        return cands[0] if cands else None

    def _source(self, f: HostField, pair) -> tuple[tuple | None, bool]:
        """(the panel whose host vertices in `f` stand for `pair`, mirrored in x).
        A sided panel the host has a record for but no vertices in this entry's
        part 0 (278's full entry has no +x mirror; its exterior has) borrows its
        partner's, mirrored."""
        if pair in f.by_pair:
            return pair, False
        partner = self._partner(pair)
        if partner is not None and partner in f.by_pair:
            return partner, True
        return None, False

    def _host_box(self, pair, f: HostField | None = None) -> Box | None:
        f = f or self.full_field
        src, flip = self._source(f, pair)
        if src is None:
            return None
        box = Box.of(f.points[n] for n in f.by_pair[src].values)
        return Box((-box.hi[0], box.lo[1], box.lo[2]), (-box.lo[0], box.hi[1], box.hi[2])) if flip else box

    def _forza_norm(self, p):
        c, s = self.forza_box.centre, self.forza_box.size
        return tuple((p[a] - c[a]) / (s[a] / 2) for a in range(3))

    def _nearest_pair(self, p_dsf, allowed: set, f: HostField, normal=None, paint: bool = False) -> tuple | None:
        """The panel of the host surface nearest to a placed point: a vote of
        the 3 host triangles (of the allowed panels) closest to it in
        normalised coordinates, by surface distance plus PANEL_FACING per unit
        of normal mismatch. Host triangles, not vertices: the host's door skins
        are large triangles with vertices only at their edges, so a point
        mid-door was nearer to vertices of the chassis behind it (door jambs,
        cabin), and the vertex vote put chassis islands into the M5's door
        skins, which tore open when crushed (testdrive3). A paint triangle
        votes among the host's paint triangles first: the outer skins are
        shared paint, while the structure a few centimetres under them (the
        front structure under the M5's wings) is trim or the host's own
        chassis texture."""
        return self._nearest_pairs([p_dsf], allowed, f, [normal], [paint])[0]

    def _nearest_pairs(self, points, allowed: set, f: HostField, normals, paints) -> list:
        """_nearest_pair for many points at once (_votes)."""
        out = [None] * len(points)
        todo = list(range(len(points)))
        ask = [i for i in todo if paints[i]]
        if ask:
            index = self._tri_index(f, allowed, paint=True)
            got = self._votes(index, f, [points[i] for i in ask], [normals[i] for i in ask], PAINT_REACH)
            for i, pair in zip(ask, got, strict=True):
                out[i] = pair
            todo = [i for i in todo if out[i] is None]
        if todo:
            got = self._votes(self._tri_index(f, allowed), f, [points[i] for i in todo], [normals[i] for i in todo])
            for i, pair in zip(todo, got, strict=True):
                out[i] = pair
        return out

    def _votes(self, index, f: HostField, points, normals, reach: float | None = None) -> list:
        """_vote for many points at once, with the same result for each.

        _vote's result depends only on the scores of the triangles it meets and on
        the rings it meets them in. A triangle binned into the cells of a box meets
        the rings dmin..dmax around a point's cell (the Chebyshev distances of a
        box's cells from a cell are contiguous), so by ring r _vote has scored it
        max(0, min(dmax, r) - dmin + 1) times. The third best is within r * cell
        exactly when that many scores, counted so, are within r * cell; and then the
        three best are among them. So per point: score the triangles with dmin <=
        rmax all at once (_closest, the same floats as closest_on_triangle), find the
        first ring r <= rmax that stops by counting, and sort only the scores within
        r * cell by (score, panel), as _vote's tuples sort. rmax runs through
        VOTE_RINGS, each round with the points the last one didn't stop; a point
        that never stops (its result is all of rings 0..7), or that has a
        non-finite score (Python's sort and numpy's differ on NaN), goes to _vote.
        The candidates of a point's cell are found per block of cells (VOTE_BLOCK)
        and scored VOTE_CHUNK at a time."""
        out: list = [None] * len(points)
        if index is None or not points:
            return out
        cell, _, _, _, tri_ids, lo, hi, rank, pairs = index
        q = np.array([self._forza_norm(p) for p in points], dtype=np.float64).reshape(-1, 3)
        has = np.array([bool(n) for n in normals])
        nrm = np.array([n if n else (0.0, 0.0, 0.0) for n in normals], dtype=np.float64).reshape(-1, 3)
        qkey = np.floor(q / cell).astype(np.int64)
        corners, tnormals = _tri_arrays(f)
        corners = tuple(c[tri_ids] for c in corners)
        tnormals = tuple(c[tri_ids] for c in tnormals)
        limit = np.array([r * cell for r in range(8)])  # _vote's r * cell
        todo, slow = np.arange(len(points)), []
        for rmax in VOTE_RINGS:
            undecided = []
            # The points' cells, sorted by block of VOTE_BLOCK^3 cells. Per block, the
            # (cell, triangle) pairs with the triangle's box within rmax rings of the cell.
            ukeys, inv = np.unique(np.hstack((qkey[todo] // VOTE_BLOCK, qkey[todo])), axis=0, return_inverse=True)
            inv = inv.reshape(-1)
            bounds = np.flatnonzero((ukeys[1:, :3] != ukeys[:-1, :3]).any(axis=1)) + 1
            ukeys = ukeys[:, 3:]
            found = []
            for k0, k1 in itertools.pairwise([0, *bounds.tolist(), len(ukeys)]):
                kk = ukeys[k0:k1]
                near = np.flatnonzero(((lo <= kk.max(axis=0) + rmax) & (hi >= kk.min(axis=0) - rmax)).all(axis=1))
                d_lo = lo[near][None, :, :] - kk[:, None, :]
                d_hi = kk[:, None, :] - hi[near][None, :, :]
                dmin = np.maximum(np.maximum(d_lo, d_hi), 0).max(axis=2)
                kp, tp = np.nonzero(dmin <= rmax)  # grouped by cell
                dmax = np.maximum(np.abs(d_lo[kp, tp]), np.abs(d_hi[kp, tp])).max(axis=1)
                found.append((kp + k0, near[tp], dmin[kp, tp], dmax))
            kp, ct, cdlo, cdhi = (np.concatenate(a) for a in zip(*found, strict=True))
            cnt = np.bincount(kp, minlength=len(ukeys))
            kstart = np.cumsum(cnt) - cnt
            # The points by cell, in chunks of about VOTE_CHUNK candidates.
            rows = np.argsort(inv, kind="stable")
            lens = cnt[inv[rows]]
            ends = np.searchsorted(np.cumsum(lens), np.arange(VOTE_CHUNK, int(lens.sum()), VOTE_CHUNK))
            for rs in np.split(rows, np.unique(ends)):
                if not len(rs):
                    continue
                sel, lk = todo[rs], inv[rs]
                lens = cnt[lk]
                first = np.cumsum(lens) - lens
                g = np.repeat(np.arange(len(sel)), lens)  # each candidate's point (in sel)
                at = np.arange(int(lens.sum())) - np.repeat(first - kstart[lk], lens)
                t, dlo, dhi = ct[at], cdlo[at], cdhi[at]
                pt = sel[g]
                d = _closest((q[pt, 0], q[pt, 1], q[pt, 2]), *(c[t] for c in corners))[0]
                dot = nrm[pt, 0] * tnormals[0][t] + nrm[pt, 1] * tnormals[1][t] + nrm[pt, 2] * tnormals[2][t]
                score = np.sqrt(d) + np.where(has[pt], PANEL_FACING * (1 - dot), 0.0)
                bad = np.zeros(len(sel), dtype=bool)
                bad[g[~np.isfinite(score)]] = True
                # The ring loop's stop: at least 3 scores within r * cell, each counted
                # once per ring up to r that the triangle touches.
                stop = np.full(len(sel), -1)
                for r in range(rmax + 1):
                    mult = np.clip(np.minimum(dhi, r) - dlo + 1, 0, None) * (score <= limit[r])
                    stop[(stop < 0) & (np.bincount(g, weights=mult, minlength=len(sel)) >= 3)] = r
                (undecided if rmax < VOTE_RINGS[-1] else slow).append(sel[(stop < 0) & ~bad])
                slow.append(sel[bad])
                # The three best at the stop ring in (score, panel) order, each as often
                # as _vote has it, within reach.
                ring = stop[g]
                mult = np.clip(np.minimum(dhi, ring) - dlo + 1, 0, None)
                pick = np.flatnonzero((ring >= 0) & ~bad[g] & (mult > 0) & (score <= limit[ring]))
                pick = pick[np.lexsort((rank[t[pick]], score[pick], g[pick]))]
                gp, mp, sp, tp = g[pick], mult[pick], score[pick], t[pick]
                acc = np.cumsum(mp)
                start = np.searchsorted(gp, gp)  # each point's first entry
                take = np.minimum(mp, 3 - (acc - mp - (acc[start] - mp[start])))
                keep = take > 0
                if reach is not None:
                    keep &= sp <= reach
                best: dict[int, list] = defaultdict(list)
                for j, s, n, c in zip(
                    gp[keep].tolist(), sp[keep].tolist(), tp[keep].tolist(), take[keep].tolist(), strict=True
                ):
                    best[j] += [(s, pairs[n])] * c
                for j, picks in best.items():
                    votes = Counter()
                    for s, pair in picks:
                        votes[pair] += 1.0 / (s + 0.005)
                    out[int(sel[j])] = votes.most_common(1)[0][0]
            todo = np.concatenate(undecided) if undecided else todo[:0]
            if not len(todo):
                break
        for i in np.concatenate(slow).tolist() if slow else ():
            out[i] = self._vote(index, f, points[i], normals[i], reach)
        return out

    def _vote(self, index, f: HostField, p_dsf, normal, reach: float | None = None) -> tuple | None:
        """The panel of the host triangles nearest to p_dsf (normalised distance plus a
        facing penalty), its three best scores weighted: the triangles of the grid
        cells ring by ring around p's cell (a triangle once per ring it touches), up
        to the first ring at which the third best is within reach of the ring."""
        if index is None:
            return None
        cell, cells, keys, members = index[:4]
        q = self._forza_norm(p_dsf)
        key = tuple(int(math.floor(q[a] / cell)) for a in range(3))
        order = at = None
        scores: dict[int, tuple] = {}  # triangle -> (score, pair)
        scored = []
        for r in range(8):
            seen = set()
            if r < 3:  # the cells of the ring's shell
                for dx, dy, dz in _SHELLS[r]:
                    seen.update(cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()))
            else:  # farther out: the occupied cells by ring (Chebyshev distance), order[at[r]:at[r + 1]]
                if order is None:
                    ring = np.abs(keys - np.array(key, dtype=np.int64)).max(axis=1)
                    near = np.flatnonzero((ring >= 3) & (ring < 8))
                    near = near[ring[near].argsort()]
                    at = (np.searchsorted(ring[near], np.arange(9))).tolist()
                    order = near.tolist()
                for i in order[at[r] : at[r + 1]]:
                    seen.update(members[i])
            new = [n for n in seen if n not in scores]
            if new:
                self._score_triangles(f, q, normal, new, scores)
            scored += (scores[n] for n in seen)
            # Cells within r of q's cell cover every triangle closer than r * cell.
            if len(scored) >= 3 and sorted(scored)[2][0] <= r * cell:
                break
        if reach is not None:
            scored = [sc for sc in scored if sc[0] <= reach]
        if not scored:
            return None
        votes = Counter()
        for score, pair in sorted(scored)[:3]:
            votes[pair] += 1.0 / (score + 0.005)
        return votes.most_common(1)[0][0]

    @staticmethod
    def _score_triangles(f: HostField, q, normal, ns: list[int], out: dict) -> None:
        """out[n] = (distance from q to triangle n + facing penalty, its pair); a few
        triangles one by one, more at once (closest_on_triangles, the same floats)."""
        if len(ns) < 24:
            for n in ns:
                t = f.tris[n]
                d, _ = closest_on_triangle(q, *t[0])
                out[n] = (math.sqrt(d) + (PANEL_FACING * (1 - _dot(normal, t[2])) if normal else 0.0), t[1])
            return
        corners, normals = _tri_arrays(f)
        index = np.array(ns, dtype=np.int64)
        d = _closest(q, *(c[index] for c in corners))[0]
        score = np.sqrt(d)
        if normal:
            nx, ny, nz = (c[index] for c in normals)
            score = score + PANEL_FACING * (1 - (normal[0] * nx + normal[1] * ny + normal[2] * nz))
        else:
            score = score + 0.0
        for n, value in zip(ns, score.tolist(), strict=True):
            out[n] = (value, f.tris[n][1])

    def _tri_index(self, f: HostField, allowed: set, paint: bool = False):
        """Host part-0 triangles of the allowed panels (paint: only the shared
        paint ones), binned by every grid cell their (normalised) bounding box
        touches."""
        key = (id(f), frozenset(allowed), "tris", paint)
        if key not in self._grids:
            cell = 0.08
            cells = defaultdict(list)
            ids, boxes = [], []
            for n, (pts, pair, _, is_paint) in enumerate(f.tris):
                if pair not in allowed or (paint and not is_paint):
                    continue
                lo = [int(math.floor(min(p[a] for p in pts) / cell)) for a in range(3)]
                hi = [int(math.floor(max(p[a] for p in pts) / cell)) for a in range(3)]
                ids.append(n)
                boxes.append(lo + hi)
                for i in range(lo[0], hi[0] + 1):
                    for j in range(lo[1], hi[1] + 1):
                        for k in range(lo[2], hi[2] + 1):
                            cells[(i, j, k)].append(n)
            # (cell, cells, the occupied cells' keys as an array, their triangle lists) for
            # _vote; for _votes the triangles (f.tris indices), their cell boxes (lo, hi),
            # the rank of their panel among the index's panels, and their panels.
            cells = dict(cells)
            boxes = np.array(boxes, dtype=np.int64).reshape(-1, 6)
            pairs = [f.tris[n][1] for n in ids]
            rank = {p: i for i, p in enumerate(sorted(set(pairs)))}
            self._grids[key] = (
                (
                    cell,
                    cells,
                    np.array(list(cells), dtype=np.int64).reshape(-1, 3),
                    list(cells.values()),
                    np.array(ids, dtype=np.int64),
                    boxes[:, :3],
                    boxes[:, 3:],
                    np.array([rank[p] for p in pairs], dtype=np.int64),
                    pairs,
                )
                if cells
                else None
            )
        return self._grids[key]

    def _side_panel(self, roles, centroid) -> tuple | None:
        """The record of `roles` on the section's side (or a centred one: a
        twin exhaust); None (the chassis) if the host has none there. A part
        never goes on the other side's record: both mirrors on one panel came
        off together, as one body centred between them (testdrive9: the S2000's
        mirror hung in the air, host 278 has no +x mirror in its full entry)."""
        cands = [p for p in self.panels_with(roles) if self._host_box(p.pair) is not None]
        if not cands:
            return None
        if abs(centroid[0]) > 0.1 * self.forza_box.size[0] / 2:
            reach = 0.1 * self.full_field.box.size[0] / 2

            def fits(p):
                box = self._host_box(p.pair)
                return (box.lo[0] < -reach and box.hi[0] > reach) or (box.centre[0] > 0) == (centroid[0] > 0)

            cands = [p for p in cands if fits(p)]
            if not cands:
                return None
        q = self._forza_norm(centroid)
        return min(cands, key=lambda p: math.dist(q, self.full_field.normalise(self._host_box(p.pair).centre))).pair

    def _glass_panel(self, which: str) -> tuple | None:
        """The host's centred glass panel (role 8) in the front (z > 0) or rear
        half: windscreen or rear window. None if the host has none there (the
        vanilla 370Z's rear window is part of its hatch)."""
        cands = [
            p
            for p in self.panels_with((8,))
            if p.pair in self.full_field.by_pair
            and abs(self._host_box(p.pair).centre[0]) < 0.3
            and (self._host_box(p.pair).centre[2] > 0) == (which == "front")
        ]
        if not cands:
            return None
        return (max if which == "front" else min)(cands, key=lambda p: self._host_box(p.pair).centre[2]).pair

    def _first(self, roles) -> tuple | None:
        for role in roles:
            for p in self.panels_with((role,)):
                if p.pair in self.full_field.by_pair or role == 1:
                    return p.pair
        return None

    def assignment(self, sections, lod: int) -> dict:
        """{(id(sub), triangle index): panel pair} for every body triangle at `lod`."""
        from dsfforza.convert import forza_materials as fm

        key = (id(sections), lod)
        if key in self._assign:
            return self._assign[key]
        f = self.full_field
        finest = min(sub.lod for s in sections for sub in s.subsections if sub.lod > 0)
        if lod != finest and (id(sections), finest) not in self._assign:
            self.assignment(sections, finest)  # decides which doors and wings are Forza parts
        out = {}
        per_section = {}
        majority: dict[str, list] = {}
        pending = []  # "nearest" sections: (name, tris, areas, components, labels)
        door_groups = set()  # majority groups of side windows
        keys_of = {}  # "nearest" section -> per triangle (vertex keys, position keys)
        cabin = []  # the `interior` section: (name, tris, normals), door cards
        for section in sections:
            kind, roles = section_rule(section.name)
            tris, shape = [], []  # (sub id, index, centroid); (unit normal, area, corner keys, paint)
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                pool = section.vertices_for(sub)
                is_paint = fm.classify(sub.material)[0] == fm.PAINT
                for k, t in enumerate(sub.triangles()):
                    pts = [self.place.point(pool[i].position) for i in t]
                    tris.append((id(sub), k, tuple(sum(p[a] for p in pts) / 3 for a in range(3))))
                    cross = _cross(_sub(pts[1], pts[0]), _sub(pts[2], pts[0]))
                    shape.append(
                        (
                            _norm(cross),
                            math.sqrt(_dot(cross, cross)) / 2,
                            [tuple(round(c * 2000) for c in p) for p in pts],
                            is_paint,
                            [(id(pool), i) for i in t],
                        )
                    )
            if not tris:
                continue
            centroid = tuple(sum(t[2][a] for t in tris) / len(tris) for a in range(3))
            pair = None
            if kind == "fixed":
                pair = self._first(roles)
            elif kind == "side":
                pair = self._side_panel(roles, centroid)
            elif kind == "glass":
                pair = self._glass_panel(roles[0])
            if kind == "glass" and pair is None:
                kind, roles = "majority", ((4, 37, 38) if roles[0] == "front" else (124, 5, 38))
            off = 0.1 * self.forza_box.size[0] / 2
            if kind == "side" and min(t[2][0] for t in tris) < -off and max(t[2][0] for t in tris) > off:
                # One section with parts on both sides (a twin exhaust, both
                # headlights in one mesh): each side's triangles take that
                # side's record, so they come off one at a time.
                halves = {s: [t for t in tris if (t[2][0] > 0) == s] for s in (True, False)}
                votes = Counter()
                for half in halves.values():
                    c = tuple(sum(t[2][a] for t in half) / len(half) for a in range(3))
                    p = self._side_panel(roles, c) or self.chassis
                    for sid, k, _ in half:
                        out[(sid, k)] = p
                    votes[p] += len(half)
                per_section[section.name] = votes
                continue
            if kind in ("fixed", "side", "glass"):
                pair = pair or self.chassis
                for sid, k, _ in tris:
                    out[(sid, k)] = pair
                per_section[section.name] = Counter({pair: len(tris)})
                if section.name.lower() == "interior":
                    cabin.append((section.name, tris, [s[0] for s in shape]))
                continue
            allowed = {p.pair for p in self.panels_with(roles)} & set(f.by_pair)
            allowed = allowed or {self.chassis}
            labels = [
                pair or self.chassis
                for pair in self._nearest_pairs(
                    [t[2] for t in tris], allowed, f, [s[0] for s in shape], [s[3] for s in shape]
                )
            ]
            if kind == "nearest":
                # Panels as connected patches of the Forza surface: a seam
                # between two panels opens when they crush differently, so
                # stray triangles and small islands join the panel around them.
                labels, merged = smooth_labels(labels, [s[1] for s in shape], [s[2] for s in shape])
                self.report.setdefault("islands_merged", {})[str(lod)] = (
                    self.report.get("islands_merged", {}).get(str(lod), 0) + merged
                )
                # Doors and wings become the Forza parts they are, once every
                # section is labelled (part_components, swinging_parts below).
                comps = part_components([s[4] for s in shape])
                pending.append((section.name, tris, [s[1] for s in shape], comps, labels))
                keys_of[section.name] = [(s[4], s[2]) for s in shape]
                continue
            votes = Counter(labels)
            picks = [(sid, k, pair, c, sh[0]) for (sid, k, c), pair, sh in zip(tris, labels, shape)]
            if kind == "majority":
                # Centred panels (boot, chassis, bumper): a left/right pair of
                # sections (taillightL/R) votes together, so both lamps land on
                # the same panel.
                group = mirror_key(section.name) if not set(roles) & set(DOORS) else section.name
                if set(roles) & set(DOORS):
                    door_groups.add(group)
                majority.setdefault(group, []).append((section.name, picks, votes))
                continue
            for sid, k, pair, _, _ in picks:
                out[(sid, k)] = pair
            per_section[section.name] = votes
        if pending:
            if lod == finest:
                swings = {p.pair for p in self.panels_with(SWINGS)}
                stats = Counter(), Counter()
                for _, _, areas, comps, labels in pending:
                    clean, total = part_stats(labels, areas, comps, swings)
                    stats[0].update(clean)
                    stats[1].update(total)
                found = {p for p in swings if stats[0][p] >= PART_MIN_AREA and stats[0][p] >= PART_SHARE * stats[1][p]}
                # Left and right alike: a door swings only if its partner on
                # the other side (if the host has one) is a Forza part too.
                self.swinging = {p for p in found if self._partner(p) is None or self._partner(p) in found}
                self.report["shut_line_parts"] = {
                    self.label(p): {
                        "part m2": round(stats[0][p], 3),
                        "labelled m2": round(stats[1][p], 3),
                        "swings": p in self.swinging,
                    }
                    for p in sorted(swings)
                    if stats[1][p]
                }
            relabelled = [
                part_relabel(labels, areas, comps, self.swinging, self.chassis)
                for _, _, areas, comps, labels in pending
            ]
            # A part's extent: its largest component. Components (and side
            # windows) that won the part but lie mostly outside its length (the
            # S5S Raptor's intake surround behind the door) stay on the body.
            span = part_spans([(t, a, c, lab) for (_, t, a, c, _), lab in zip(pending, relabelled)], self.swinging)
            swings = {p.pair for p in self.panels_with(SWINGS)}
            for (name, tris, areas, comps, before), labels in zip(pending, relabelled):
                labels = part_trim(tris, areas, comps, labels, span, self.chassis)
                tears = self.report.setdefault("door_tears_m", {}).setdefault(str(lod), [0.0, 0.0])
                tears[0] += tear_length(before, keys_of[name], swings)
                tears[1] += tear_length(labels, keys_of[name], self.swinging)
                for (sid, k, _), pair in zip(tris, labels):
                    out[(sid, k)] = pair
                per_section[name] = Counter(labels)
        else:
            span = {}
        # Door cards go with their swinging door, as vanilla door cards are
        # skinned to the door records (cockpit.md): an open door shows its
        # card, and no card is left standing in the opening.
        doors = [d for d in span if self.panels[d].role in DOORS]
        for name, tris, normals in cabin:
            for (sid, k, c), normal in zip(tris, normals):
                hit = [d for d in doors if in_door_card(c, normal, span[d])]
                if hit:
                    out[(sid, k)] = hit[0]
            per_section[name] = Counter(out[(sid, k)] for sid, k, _ in tris)
        for group, members_ in majority.items():
            total = sum((v for _, _, v in members_), Counter())
            pair = total.most_common(1)[0][0]
            # Side windows: the part of the glass within a swinging door's
            # length goes with that door (the S5S Raptor's door window and
            # quarter window are one Forza section), the rest stays put.
            doors = [d for d in span if self.panels[d].role in DOORS] if group in door_groups else []
            for name, picks, _ in members_:
                for sid, k, _, c, normal in picks:
                    side = normal is not None and abs(normal[0]) >= 0.5  # not a sunroof or T-top
                    inside = [d for d in doors if side and within_span(c, span[d])]
                    if inside:
                        out[(sid, k)] = inside[0]
                    elif pair in span and not (side and within_span(c, span[pair])):
                        out[(sid, k)] = self.chassis
                    else:
                        out[(sid, k)] = pair
                per_section[name] = Counter(out[(sid, k)] for sid, k, *_ in picks)
        self._assign[key] = out
        members: dict[tuple, set] = defaultdict(set)
        sub_owner = {id(sub): id(section) for section in sections for sub in section.subsections}
        for (sid, _), pair in out.items():
            members[pair].add(sub_owner[sid])
        self._members[key] = members
        self.report.setdefault("sections", {})[str(lod)] = {
            name: {self.label(p): n for p, n in votes.items()} for name, votes in per_section.items()
        }
        return out

    def label(self, pair) -> str:
        p = self.panels.get(pair)
        return f"{pair[0]},{pair[1]} {role_name(p.role) if p else '?'}"

    # -- geometry --
    def _geometry(self, builder, sections, lod, f: HostField, record_box: bool = False, **kwargs) -> dict[tuple, list]:
        """Per panel: add_geometry over that panel's triangles, then s9, s12 and
        normal deltas written into the new vertices."""
        assign = self.assignment(sections, lod)
        by_pair = self._members[(id(sections), lod)]
        kwargs.pop("glass_links", None)  # panels replace the glass links
        out = {}
        if record_box and self._boxes_lod is not None and lod > self._boxes_lod:
            record_box = False  # keep the boxes of the finest full-entry LOD
        if record_box and self._boxes_lod != lod:
            self.boxes.clear()
            self._boxes_lod = lod
        self._seam = self._seam_map(sections, lod, assign)
        self._pins = self._pin_map(f)
        kwargs["exposed"] = True  # split_exposed's subsections: UV0/sky from the host (_expose)
        for pair in sorted(by_pair):
            secs = [s for s in sections if id(s) in by_pair[pair]]
            # binds this pair now, not the loop variable
            keep = (lambda pr: lambda section, sub, k, tri: assign.get((id(sub), k)) == pr)(pair)  # noqa: PLC3002
            subs = self.f2d.add_geometry(builder, secs, lod, self.place, wheel=False, keep=keep, **kwargs)
            pts = self._finish(builder, subs, pair, f)
            if record_box:
                self.boxes[pair] += pts
            if subs:
                out[pair] = subs
                self.drawn.add(pair)
        self.report["seam_positions"] = len(self._seam_written)
        return out

    def _rigid(self, pair) -> bool:
        """A panel that only crushes on a converted car (chassis, front
        structure, doors, wings), so its seams with others can be kept
        shut; bound() keeps it from swinging."""
        p = self.panels.get(pair)
        return p is not None and p.role in BODY

    # Doors and wings swing on vanilla cars. Their hinge limit is 0, but
    # forceOpenPanels opens vanilla doors wide (run host-20261023-093807: 227,
    # 266) and the converted RX-7's and Mustang's door panels too (run
    # host-20261023-101158), and a crash knocks them ajar. A converted door or
    # wing is a patch of the one-piece Forza skin cut along the host's panel
    # layout, not a Forza shut line, with no jamb or door card behind it (the
    # door cards are on the chassis): swung, its border opens into a jagged
    # slit onto the hollow body (testdrive9: the S5S Raptor behind its door,
    # the 250 GTO across its nose, where both wings meet with no bumper
    # section between them). Pinned seams (_pin_map) only hold while the
    # panels share a transform. So every rigid panel's vertices take the
    # chassis' transform slot (s9 byte 0) and keep their own panel-data index
    # (byte 1): they still dent with their own damage amount, and never swing.
    # A door or wing that is a Forza part (self.swinging, assignment) has the
    # Forza shut line as its border and keeps its own slot: it swings as vanilla.

    def bound(self, pair) -> tuple:
        """s9 bytes 0, 1 written on a panel's vertices: (transform slot, panel
        data index). Rigid panels use the chassis' slot, unless they swing."""
        return (self.chassis[0], pair[1]) if self._rigid(pair) and pair not in self.swinging else pair

    def _seam_map(self, sections, lod: int, assign: dict) -> dict:
        """Quantised position (int16 x3, as encoded) -> the rigid panels whose
        triangles use it, for positions on more than one."""
        from dsfforza.convert import forza_materials as fm

        users = defaultdict(set)
        self._seam_paint = set()
        for section in sections:
            for sub in section.subsections:
                if sub.lod != lod:
                    continue
                pool = section.vertices_for(sub)
                paint = fm.classify(sub.material)[0] == fm.PAINT
                for k, t in enumerate(sub.triangles()):
                    pair = assign.get((id(sub), k))
                    if pair is None or not self._rigid(pair):
                        continue
                    for i in t:
                        p = self.place.point(pool[i].position)
                        key = tuple(max(-32768, min(32767, round(c * POSITION_UNITS))) for c in p)
                        users[key].add(pair)
                        if paint:
                            self._seam_paint.add(key)
        # Near-coincident positions of different panels (separate Forza shells
        # that touch) are seams too: such positions are grouped, and every
        # member takes the group's panels and its first position's crush.
        near = SEAM_NEAR * POSITION_UNITS
        cell = defaultdict(list)
        for key in users:
            cell[tuple(int(c // near) for c in key)].append(key)
        parent = {}

        def find(k):
            while parent.get(k, k) != k:
                parent[k] = parent.get(parent[k], parent[k])
                k = parent[k]
            return k

        for key, pairs in users.items():
            ck = tuple(int(c // near) for c in key)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for other in cell.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                            if other != key and users[other] != pairs and math.dist(key, other) <= near:
                                a, b = find(key), find(other)
                                if a != b:
                                    parent[max(a, b)] = min(a, b)
        groups = defaultdict(set)
        for key in list(parent):
            root = find(key)
            groups[root].update((key, root))
        self._seam_rep = {}
        for root, members in groups.items():
            pairs = set().union(*(users[k] for k in members))
            paint = any(k in self._seam_paint for k in members)
            for k in members:
                users[k] = pairs
                self._seam_rep[k] = root
                if paint:
                    self._seam_paint.add(k)
        return {k: tuple(sorted(v)) for k, v in users.items() if len(v) > 1}

    # Seams that stay shut whatever the damage amounts (testdrive7: the S5S
    # Raptor's door panel border ran through the rear quarter and the front
    # wing as a sawtooth of Forza triangle edges, and opened into jagged black
    # slits after a side hit). A seam vertex is drawn at p + f * d on each
    # side, f being that panel's damage amount; sharing d closes the seam only
    # when both panels get the same f, which fullVisualDamage does and a real
    # impact doesn't. Only d = 0 keeps it shut for any f. Vanilla cars do that:
    # the vertices where doors, wings, sills and the chassis meet barely move
    # (median 0-3 mm on 147, 163, 227), while their middles dent; where they
    # move (wings against the front structure, 0.2-0.3 m on 163), the seam is
    # under the bonnet and backed by structure. Forza skins have no backing,
    # so seams between rigid panels on body paint are always pinned (d = 0;
    # 163's wing/front-structure border sits on the S5S Raptor's arch flare),
    # trim seams where the host's border stays still too, and the crush
    # fades in over SEAM_REACH. Other trim seams keep the shared d.

    def _border_motion(self, f: HostField) -> dict:
        """{frozenset of the two roles: median crush (m) of the host's vertices
        where panels of those roles meet} over the host's rigid panels."""
        key = id(f)
        if key in self._motion:
            return self._motion[key]
        cells = defaultdict(list)
        rigid = [n for n, pr in enumerate(f.pairs) if self._rigid(pr)]
        for n in rigid:
            cells[tuple(int(math.floor(c / SEAM_BORDER)) for c in f.points[n])].append(n)
        moved = defaultdict(list)
        for n in rigid:
            ck = tuple(int(math.floor(c / SEAM_BORDER)) for c in f.points[n])
            best = None
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for m in cells.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                            if f.pairs[m] == f.pairs[n]:
                                continue
                            d = math.dist(f.points[m], f.points[n])
                            if d <= SEAM_BORDER and (best is None or d < best[0]):
                                best = (d, m)
            if best is not None:
                roles = frozenset((self.panels[f.pairs[n]].role, self.panels[f.pairs[best[1]]].role))
                moved[roles].append(math.sqrt(_dot(f.disp[n], f.disp[n])))
        out = {k: sorted(v)[len(v) // 2] for k, v in moved.items()}
        self._motion[key] = out
        self.report.setdefault(
            "host_border_motion", {" | ".join(sorted(role_name(r) for r in k)): round(v, 3) for k, v in out.items()}
        )
        return out

    def _pinned(self, a, b, motion: dict) -> bool:
        """Is the seam between rigid panels a and b pinned: the host's border
        of these two roles stays still, or the host has no such border."""
        m = motion.get(frozenset((self.panels[a].role, self.panels[b].role)))
        return m is None or m <= SEAM_STILL

    def _pin_map(self, f: HostField) -> dict:
        """Pair -> (cells of SEAM_REACH: placed points) of its pinned seam positions."""
        motion = self._border_motion(f)
        pins = defaultdict(lambda: defaultdict(list))
        count = 0
        for key, pairs in self._seam.items():
            if key not in self._seam_paint and not any(
                self._pinned(a, b, motion) for a in pairs for b in pairs if a < b
            ):
                continue
            p = tuple(c / POSITION_UNITS for c in key)
            ck = tuple(int(math.floor(c / SEAM_REACH)) for c in p)
            for pair in pairs:
                pins[pair][ck].append(p)
            count += 1
        self.report.setdefault("pinned_seam_positions", []).append(count)
        return pins

    def _pin_weight(self, pair, p) -> float:
        """0 on a pinned seam of `pair`, rising smoothly to 1 at SEAM_REACH from it."""
        cells = self._pins.get(pair)
        if not cells:
            return 1.0
        ck = tuple(int(math.floor(c / SEAM_REACH)) for c in p)
        best = SEAM_REACH
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for q in cells.get((ck[0] + dx, ck[1] + dy, ck[2] + dz), ()):
                        d = math.dist(p, q)
                        best = min(best, d)
        t = best / SEAM_REACH
        return t * t * (3 - 2 * t)

    def _disp(self, f: HostField, pair, p) -> tuple:
        """Crush displacement (s12 - position) at placed point p: the host
        panel's, inverse-distance weighted over its nearest vertices in
        normalised coordinates, scaled by the car size ratio."""
        cache = self.__dict__.setdefault("_disp_cache", {})
        key = (id(f), pair, p)
        if key not in cache:
            src, flip = self._source(f, pair)
            grid = f.by_pair[src] if src is not None else f.all_grid
            q = self._forza_norm(p)
            hits = grid.nearest((-q[0], q[1], q[2]) if flip else q, self.opts.k)
            cache[key] = self._weighted_disp(f, grid, flip, hits, f.box.scale_to(self.forza_box))
        return cache[key]

    @staticmethod
    def _weighted_disp(f: HostField, grid: Grid, flip: bool, hits, scale) -> tuple:
        """_disp from the nearest host vertices (grid.nearest's hits)."""
        wsum, d = 0.0, [0.0, 0.0, 0.0]
        for dist, n in hits:
            w = 1.0 / (dist + 0.02) ** 2
            disp = f.disp[grid.values[n]]
            if flip:
                disp = (-disp[0], disp[1], disp[2])
            for a in range(3):
                d[a] += w * disp[a]
            wsum += w
        return tuple(d[a] / wsum * scale[a] for a in range(3)) if wsum else (0.0, 0.0, 0.0)

    def _disp_prefetch(self, f: HostField, keys) -> None:
        """_disp for many (pair, p) at once into its cache (Grid.nearest_many per
        host grid; the weighting is _disp's)."""
        cache = self.__dict__.setdefault("_disp_cache", {})
        groups = defaultdict(dict)  # (source panel, flip) -> {p: [pairs]}
        for pair, p in keys:
            if (id(f), pair, p) not in cache:
                groups[self._source(f, pair)].setdefault(p, []).append(pair)
        scale = f.box.scale_to(self.forza_box) if groups else None
        for (src, flip), todo in groups.items():
            grid = f.by_pair[src] if src is not None else f.all_grid
            ps = list(todo)
            qs = [self._forza_norm(p) for p in ps]
            hits = grid.nearest_many([(-q[0], q[1], q[2]) if flip else q for q in qs], self.opts.k)
            for p, h in zip(ps, hits, strict=True):
                value = self._weighted_disp(f, grid, flip, h, scale)
                for pair in todo[p]:
                    cache[(id(f), pair, p)] = value

    def _exposed_box(self, tag: str, pair, p) -> Box | None:
        """Projection box of an exposed vertex: the bay's, or its panel's (bonnet,
        else boot lid, by the panel's role or the nearer one)."""
        kind = _kind_of(tag)
        if kind != "inner":
            return self.exposed_boxes.get(kind)
        role = self.panels[pair].role if pair in self.panels else None
        name = "trunk" if role in (5, 124) else "hood" if role == 4 else None
        boxes = {k: b for k, b in self.exposed_boxes.items() if k in ("hood", "trunk")}
        if name is None and boxes:
            name = min(boxes, key=lambda k: abs(boxes[k].centre[2] - p[2]))
        return boxes.get(name)

    def _expose(self, builder, sub, pair, f: HostField) -> None:
        """UV0 and baked sky for the vertices add_geometry marked with an
        EXPOSED_SENTINEL (split_exposed's inner faces and bay trims): a planar
        projection of the vertex in its box onto the tag's picture of the shared
        chassis texture (EXPOSED_MAP), and exposed_sky."""
        stride, buf = sub.stride, builder.buffers[sub.stride]
        tags = {struct.pack("<2e", *uv): tag for tag, uv in EXPOSED_SENTINEL.items()}
        first, count = sub.first_index, sub.index_count
        stats = self.report.setdefault("exposed", Counter())
        for v in sorted({sub.base_vertex + i for i in builder.indices[first : first + count]}):
            at = v * stride
            tag = tags.get(bytes(buf[at + 8 : at + 12]))
            if tag is None:
                continue
            p = tuple(c / POSITION_UNITS for c in struct.unpack_from("<3h", buf, at))
            box = self._exposed_box(tag, pair, p)
            rect, au, av = EXPOSED_MAP[tag]
            if box is None:
                uv = ((rect[0] + rect[1]) / 2, (rect[2] + rect[3]) / 2)
            else:
                t = [
                    min(1.0, max(0.0, (p[a] - box.lo[a]) / box.size[a])) if box.size[a] > 1e-3 else 0.5
                    for a in (au, av)
                ]
                uv = (
                    rect[0] + t[0] * (rect[1] - rect[0]),
                    rect[3] - t[1] * (rect[3] - rect[2]) if av == 1 else rect[2] + t[1] * (rect[3] - rect[2]),
                )
            struct.pack_into("<2e", buf, at + 8, *uv)
            sky = exposed_sky(tag, stored_normal(buf, at + LAYOUT[stride][1]))
            struct.pack_into("<h", buf, at + 6, round(sky * POSITION_UNITS))
            stats[_kind_of(tag) + " vertices"] += 1

    def _is_local(self, f: HostField, material: bytes) -> bool:
        """A material of the entry itself (Chassis/Lights) rather than a shared one."""
        if material in f.local:
            return True
        cached = f.__dict__.get("_shared_materials")  # (uv1 it was made from, set)
        if cached is None or cached[0] is not f.uv1:
            shared = set(self.f2d.SHARED.values()) | set(self.f2d.SHARED_EXTERIOR.values())
            if f.uv1 is not None:
                shared |= {t[3] for t in f.uv1.tris if not t[4]}
            cached = f._shared_materials = (f.uv1, shared)
        return material not in cached[1]

    def _uv1(
        self, f: HostField, p, pair, material: bytes, any_panel: bool = False, normal=None, chart: int | None = None
    ):
        """(UV1, host chart) or None."""
        key = (id(f), pair, material, p, any_panel, normal, chart)
        cache = self.__dict__.setdefault("_uv1_cache", {})
        if key not in cache:
            cache[key] = f.uv1.lookup(
                self._forza_norm(p), pair, material, self._is_local(f, material), any_panel, normal, chart
            )
        return cache[key]

    def _uv1_prefetch(self, f: HostField, items) -> None:
        """_uv1 for many (p, pair, material, any_panel, normal, chart) at once into
        its cache (Uv1Field.lookup_many)."""
        cache = self.__dict__.setdefault("_uv1_cache", {})
        todo = {}
        for p, pair, material, any_panel, normal, chart in items:
            key = (id(f), pair, material, p, any_panel, normal, chart)
            if key not in cache and key not in todo:
                todo[key] = (
                    self._forza_norm(p),
                    pair,
                    material,
                    self._is_local(f, material),
                    any_panel,
                    normal,
                    chart,
                )
        if todo:
            for key, hit in zip(todo, f.uv1.lookup_many(list(todo.values())), strict=True):
                cache[key] = hit

    def _prefetch(self, builder, subs, pair, f: HostField) -> None:
        """Fill the _disp and _uv1 caches with what _finish asks of them, at once.
        _finish only reads positions, s9 byte 2 and the s5 normal (bytes 0, 1)
        before it writes them, and writes none of these."""
        rigid = self._rigid(pair)
        disp, uv1 = [], []
        for sub in subs:
            if sub.stride not in LAYOUT:
                continue
            o9, o5, _ = LAYOUT[sub.stride]
            buf, stride = builder.buffers[sub.stride], sub.stride
            idx = builder.indices[sub.first_index : sub.first_index + sub.index_count]
            for v in sorted({sub.base_vertex + i for i in idx}):
                at = v * stride
                raw = struct.unpack_from("<3h", buf, at)
                p = tuple(c / POSITION_UNITS for c in raw)
                others = self._seam.get(raw) if rigid else None
                if others and pair in others:
                    at_p = tuple(c / POSITION_UNITS for c in self._seam_rep.get(raw, raw))
                    disp += ((q, at_p) for q in others)
                else:
                    disp.append((pair, p))
                if stride == 32 and f.uv1 is not None:
                    material = sub.material
                    if buf[at + o9 + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    uv1.append((p, pair, material, False, stored_normal(buf, at + o5), None))
        self._disp_prefetch(f, disp)
        if uv1:
            self._uv1_prefetch(f, uv1)

    def _finish(self, builder, subs, pair, f: HostField) -> list:
        """Write s9 bytes 0-1, s12, the s5 normal delta and (32-byte vertices) the
        decal-atlas UV1 of the vertices `subs` use."""
        points = []
        rigid = self._rigid(pair)
        bound = self.bound(pair)
        self._prefetch(builder, subs, pair, f)
        for sub in subs:
            if sub.stride not in LAYOUT:
                continue
            self._expose(builder, sub, pair, f)
            o9, o5, o12 = LAYOUT[sub.stride]
            buf = builder.buffers[sub.stride]
            stride = sub.stride
            idx = builder.indices[sub.first_index : sub.first_index + sub.index_count]
            verts = sorted({sub.base_vertex + i for i in idx})
            pos, dmg, charts = {}, {}, {}
            for v in verts:
                at = v * stride
                raw = struct.unpack_from("<3h", buf, at)
                p = tuple(c / POSITION_UNITS for c in raw)
                pos[v] = p
                # A position shared with other never-swinging panels (a seam inside
                # one Forza surface) gets the same crushed position on each, so the
                # seam stays shut when the panels crush alike.
                others = self._seam.get(raw) if rigid else None
                if others and pair in others:
                    at_p = tuple(c / POSITION_UNITS for c in self._seam_rep.get(raw, raw))
                    ds = [self._disp(f, q, at_p) for q in others]
                    disp = tuple(sum(d[a] for d in ds) / len(ds) for a in range(3))
                    self._seam_written.add((id(f), raw))
                else:
                    disp = self._disp(f, pair, p)
                if rigid:
                    # Every copy of a seam position takes the same weight (the
                    # smallest of its panels'), so unpinned seams stay shut too.
                    w = (
                        min(self._pin_weight(q, at_p) for q in others)
                        if others and pair in others
                        else self._pin_weight(pair, p)
                    )
                    disp = tuple(w * c for c in disp)
                q = tuple(p[a] + disp[a] for a in range(3))
                dmg[v] = q
                buf[at + o9], buf[at + o9 + 1] = bound
                struct.pack_into("<4e", buf, at + o12, q[0], q[1], q[2], 1.0)
                if stride == 32 and f.uv1 is not None:
                    # Lights-material triangles that aren't lamps (badges, plate,
                    # reflector trim; s9 byte 2 = 0) take the panel's paint layout:
                    # the lamp layout puts them on the broken-lamp tile, whose dark
                    # blobs strip the paint to white when damaged.
                    material = sub.material
                    if buf[at + o9 + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    hit = self._uv1(f, p, pair, material, normal=stored_normal(buf, at + o5))
                    if hit is not None:
                        struct.pack_into("<2e", buf, at + 12, *hit[0])  # semantic 8 components 2, 3
                        charts[v] = hit[1]
            points += pos.values()
            # Normal deltas: rotate the stored normal by the change of the
            # area-weighted face normal from the intact to the crushed mesh.
            n0 = defaultdict(lambda: [0.0, 0.0, 0.0])
            n1 = defaultdict(lambda: [0.0, 0.0, 0.0])
            for t in range(0, len(idx) - 2, 3):
                tri = [sub.base_vertex + idx[t + j] for j in range(3)]
                a = _cross(_sub(pos[tri[1]], pos[tri[0]]), _sub(pos[tri[2]], pos[tri[0]]))
                b = _cross(_sub(dmg[tri[1]], dmg[tri[0]]), _sub(dmg[tri[2]], dmg[tri[0]]))
                for v in tri:
                    for k in range(3):
                        n0[v][k] += a[k]
                        n1[v][k] += b[k]
            for v in verts:
                at = v * stride
                b0, b1 = buf[at + o5], buf[at + o5 + 1]
                delta = normal_delta(b0, b1, _norm(n0[v]), _norm(n1[v]))
                buf[at + o5 + 2], buf[at + o5 + 3] = delta
            if charts:
                self._split_seams(builder, sub, f, pair, pos, charts)
        return points

    def _split_seams(self, builder, sub, f: HostField, pair, pos, charts) -> None:
        """A triangle whose corners took UV1 from different host charts would
        stretch across the decal atlas (scratch rows between the tiles show as
        long streaks). Put the triangle on the chart most of its corners use:
        the other corners get a copy of their vertex with UV1 from that chart."""
        buf = builder.buffers[sub.stride]
        stride, o5 = sub.stride, LAYOUT[sub.stride][1]
        # The lookups below at once (_uv1_prefetch): the loop only appends vertices
        # and changes the indices of the triangle it is at.
        asks = {}
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            tri = [sub.base_vertex + builder.indices[t + j] for j in range(3)]
            cs = [charts.get(v) for v in tri]
            if None in cs or cs[0] == cs[1] == cs[2]:
                continue
            chart = Counter(cs).most_common(1)[0][0]
            for j, v in enumerate(tri):
                if cs[j] != chart and (v, chart) not in asks:
                    material = sub.material
                    if buf[v * stride + LAYOUT[stride][0] + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT
                    asks[(v, chart)] = (pos[v], pair, material, False, stored_normal(buf, v * stride + o5), chart)
        if asks:
            self._uv1_prefetch(f, list(asks.values()))
        copies: dict[tuple, int | None] = {}
        for t in range(sub.first_index, sub.first_index + sub.index_count, 3):
            tri = [sub.base_vertex + builder.indices[t + j] for j in range(3)]
            cs = [charts.get(v) for v in tri]
            if None in cs or cs[0] == cs[1] == cs[2]:
                continue
            chart = Counter(cs).most_common(1)[0][0]
            for j, v in enumerate(tri):
                if cs[j] == chart:
                    continue
                key = (v, chart)
                if key not in copies:
                    copies[key] = None
                    material = sub.material
                    if buf[v * stride + LAYOUT[stride][0] + 2] == 0 and self._is_local(f, material):
                        material = self.f2d.PAINT  # as in _finish: non-lamp Lights triangles
                    hit = self._uv1(f, pos[v], pair, material, normal=stored_normal(buf, v * stride + o5), chart=chart)
                    new = len(buf) // stride
                    why = (
                        "no hit"
                        if hit is None
                        else "other chart"
                        if hit[1] != chart
                        else "16-bit range"
                        if new - sub.base_vertex > 0xFFFF
                        else None
                    )
                    if why:
                        stats = self.report.setdefault("seam_skipped", {})
                        stats[why] = stats.get(why, 0) + 1
                    if why is None:
                        buf += buf[v * stride : (v + 1) * stride]
                        struct.pack_into("<2e", buf, new * stride + 12, *hit[0])
                        copies[key] = new
                        self.report["seam_vertices"] = self.report.get("seam_vertices", 0) + 1
                if copies[key] is not None:
                    builder.indices[t + j] = copies[key] - sub.base_vertex

    def _merge(self, builder, panel_subs: dict[tuple, list]) -> list:
        """Part-0 submodels: every panel's triangles again, one submodel per
        material (split when the vertex span passes 16 bits), on the same vertices."""
        by_material = defaultdict(list)
        for pair in sorted(panel_subs):
            for s in panel_subs[pair]:
                by_material[s.material].append(s)
        merged = []
        for material, subs in by_material.items():
            groups, current = [], []
            lo = hi = None
            for s in sorted(subs, key=lambda s: s.base_vertex):
                idx = builder.indices[s.first_index : s.first_index + s.index_count]
                s_lo, s_hi = s.base_vertex + min(idx), s.base_vertex + max(idx)
                if current and max(hi, s_hi) - min(lo, s_lo) > 0xFFFF:
                    groups.append((lo, current))
                    current, lo, hi = [], None, None
                current.append(s)
                lo = s_lo if lo is None else min(lo, s_lo)
                hi = s_hi if hi is None else max(hi, s_hi)
            if current:
                groups.append((lo, current))
            for base, group in groups:
                first = len(builder.indices)
                for s in group:
                    for i in builder.indices[s.first_index : s.first_index + s.index_count]:
                        builder.indices.append(s.base_vertex + i - base)
                new = self.f2d.NewSub(
                    builder.new_uid(),
                    material,
                    group[0].stride,
                    base,
                    first,
                    len(builder.indices) - first,
                    group[0].flag,
                )
                builder.subs.append(new)
                merged.append(new)
        return merged

    def full_lod(self, builder, part_lods: dict, mask: int, lod: int, **kwargs) -> None:
        """Full entry, one detail mask: per-panel MODL parts plus the merged part 0."""
        panel_subs = self._geometry(builder, self.car.body, lod, self.full_field, record_box=True, **kwargs)
        for pair, subs in panel_subs.items():
            part = self.panels[pair].part if pair in self.panels else None
            if part is not None and part != 0:
                part_lods.setdefault(part, {}).setdefault(mask, []).extend(subs)
        part_lods.setdefault(0, {})[mask] = self._merge(builder, panel_subs)
        self.report["panels"].setdefault(
            str(mask), {self.label(p): sum(s.index_count // 3 for s in subs) for p, subs in panel_subs.items()}
        )

    def exterior(self, builder, lod: int, **kwargs) -> list:
        """Exterior entry: the merged body only (vanilla exteriors have no panel parts)."""
        panel_subs = self._geometry(builder, self.car.body, lod, self.ext_field, **kwargs)
        merged = self._merge(builder, panel_subs)
        drop = {id(s) for subs in panel_subs.values() for s in subs}
        compact(builder, [s for s in builder.subs if id(s) not in drop])
        return merged

    def interior_uv1(self, stream, interior) -> int:
        """Decal-atlas UV1 for the rebuilt interior entry's 32-byte vertices (its
        outer shell's paint: the bonnet seen from the cockpit), from the host
        interior's shell by nearest triangle of the same material class, any
        panel. Call after the interior is rebuilt. Returns vertices written."""
        from dsfforza.convert.cockpit import DASHBOARD_INSTRUMENT

        if self.int_uv1 is None:
            return 0
        # One field for the plan: _uv1's cache keys on id(f), which a field made per call could reuse.
        f = self.__dict__.get("_int_field")
        if f is None:
            f = self._int_field = HostField([], [], [], self.int_uv1.box, uv1=self.int_uv1, local=self.int_local)
        drsp = child(child(interior, "MDXN"), "DRSP")
        buffers, index_buffers, users = {}, {}, defaultdict(dict)
        for e in drsp.children:
            if e.tag == "VXCK":
                hd = payload(stream, child(e, "VXHD"))
                stride = struct.unpack_from("<I", hd, 12)[0]
                elements = vm.parse_vdcl(payload(stream, child(e, "VDCL")))
                e8 = next((x for x in elements if x.semantic == 8), None)
                e5 = next((x for x in elements if x.semantic == 5), None)
                # Not the needle buffers: their semantic 8 is DashboardInstrument
                # (u, v, gauge id, 0), and a UV1 there overwrites the gauge id,
                # so the engine never turns the needles.
                if (
                    e8 is not None
                    and e8.components == 4
                    and e8.type == 8
                    and elements[0].semantic == 0
                    and e8.name_hash != DASHBOARD_INSTRUMENT
                ):
                    # Offsets from the VDCL, not the converter's layout: the
                    # interior may keep host-shaped buffers (some lack semantic 9).
                    buffers[hd[:8]] = (child(e, "VXDT"), stride, e8.offset + 4, e5.offset if e5 else None)
            elif e.tag == "IXCK":
                index_buffers[payload(stream, child(e, "IXHD"))[:8]] = payload(stream, child(e, "IXDT"))
        for e in drsp.children:
            if e.tag != "SBMC":
                continue
            d = payload(stream, child(e, "SBBH"))
            if d[16:24] not in buffers:
                continue
            (base,) = struct.unpack_from("<I", d, 0x20)
            first, count = struct.unpack_from("<II", d, 0x38)
            ib = index_buffers[d[0x30:0x38]]
            for t in range(first, first + count):
                users[d[16:24]][base + struct.unpack_from("<H", ib, t * 2)[0]] = d[8:16]
        written = 0
        for uid, (vxdt, stride, o_uv1, o5) in buffers.items():
            buf = bytearray(payload(stream, vxdt))
            asks = []  # the lookups below at once (they write UV1 only, which none reads)
            for v, material in users[uid].items():
                at = v * stride
                p = tuple(c / POSITION_UNITS for c in struct.unpack_from("<3h", buf, at))
                normal = stored_normal(buf, at + o5) if o5 is not None else None
                asks.append((p, (0, 0), material, True, normal, None))
            self._uv1_prefetch(f, asks)
            for v, material in users[uid].items():
                at = v * stride
                p = tuple(c / POSITION_UNITS for c in struct.unpack_from("<3h", buf, at))
                normal = stored_normal(buf, at + o5) if o5 is not None else None
                hit = self._uv1(f, p, (0, 0), material, any_panel=True, normal=normal)
                if hit is not None:
                    struct.pack_into("<2e", buf, at + o_uv1, *hit[0])
                    written += 1
            vxdt.payload = bytes(buf)
        self.report["interior_uv1"] = written
        return written

    # -- hierarchy --
    def patch_upvh(self, data: bytes) -> tuple[bytes, dict]:
        """Move each panel's hinge pivot, rigid body (centre, inertia) and
        collision boxes from the host panel's box to the Forza panel's, by
        relative position in the box. With opts.hull, scale the root hull too."""
        h = upvh_mod.parse(data)
        out = bytearray(data)
        report = {}
        pdl = h.pdl_offset
        for pair, pts in sorted(self.boxes.items()):
            panel = self.panels.get(pair)
            hb, fb = self._host_box(pair), robust_box(pts)
            if panel is None or hb is None or fb is None:
                continue
            for r in panel.records:
                if r.role in NOT_MOVED or r.type != 1:
                    continue
                if r.role == 38 and not self.opts.hull:
                    continue  # the chassis body/box is the car's; only with the hull option
                entry = {"role": role_name(r.role)}
                # Centred hinges and bodies stay centred while the Forza panel
                # spans the centre line too; a host twin exhaust taken by the
                # RX-7's one-sided pipe gets its body moved onto the pipe.
                centred = fb.lo[0] < 0 < fb.hi[0]
                if r.hinge >= 0:
                    at = h.offsets["hinges"] + r.hinge * 0x30
                    old = struct.unpack_from("<3f", out, at)
                    new = hb.to(fb, old)
                    if abs(old[0]) < 0.02 and centred:
                        new = (old[0],) + new[1:]  # keep centred hinges centred
                    if math.dist(old, new) > MAX_HINGE_MOVE:
                        entry["hinge_kept"] = round(math.dist(old, new), 3)  # implausible: keep the host's
                        new = old
                    struct.pack_into("<3f", out, at, *new)
                    entry["hinge"] = [round(c, 3) for c in new]
                    entry["hinge_moved"] = round(math.dist(old, new), 3)
                scale = tuple(min(COLLISION_SCALE[1], max(COLLISION_SCALE[0], c)) for c in hb.scale_to(fb))
                if r.body >= 0:
                    at = h.offsets["bodies"] + r.body * 0xB0
                    m = struct.unpack_from("<12f", out, at)
                    rows = (m[0:3], m[4:7], m[8:11])
                    pos = struct.unpack_from("<3f", out, at + 0x30)
                    centre = tuple(-_dot(rows[a], pos) for a in range(3))
                    new_c = hb.to(fb, centre)
                    if abs(centre[0]) < 0.02 and centred:
                        new_c = (centre[0],) + new_c[1:]
                    new_pos = tuple(-sum(rows[k][a] * new_c[k] for k in range(3)) for a in range(3))
                    struct.pack_into("<3f", out, at + 0x30, *new_pos)
                    inertia = struct.unpack_from("<3f", out, at + 0x40)
                    new_i = (
                        inertia[0] * (scale[1] ** 2 + scale[2] ** 2) / 2,
                        inertia[1] * (scale[0] ** 2 + scale[2] ** 2) / 2,
                        inertia[2] * (scale[0] ** 2 + scale[1] ** 2) / 2,
                    )
                    struct.pack_into("<3f", out, at + 0x40, *new_i)
                    entry["centre"] = [round(c, 3) for c in new_c]
                if r.collision >= 0:
                    _scale_collision(out, pdl + 0x10 + r.collision, scale)
                    entry["collision_scale"] = [round(s, 3) for s in scale]
                report[f"record {r.index}"] = entry
        out, report["mirrors"] = self._remount_mirrors(bytes(out))
        out = bytearray(out)
        if self.opts.hull:
            root = h.records[0]
            if root.collision >= 0:
                host_box, forza_box = trimmed_box(self.full_field.points), self.forza_hull_box
                at = pdl + 0x10 + root.collision
                floor = _collision_bottom(out, at)
                _map_collision(out, at, host_box, forza_box)
                raised = _floor_collision(out, at, floor)
                report["hull"] = {
                    k: [[round(c, 3) for c in b.lo], [round(c, 3) for c in b.hi]]
                    for k, b in (("host", host_box), ("forza", forza_box))
                }
                if raised:
                    report["hull"]["bottom_raised"] = round(raised, 3)
        self.report["upvh"] = report
        return bytes(out), report

    # A side mirror's record hangs under the record of what the mirror is mounted on,
    # and moves with it: vanilla mirrors sit on the door skin and hang under the door
    # record (253 of 277), the rest under the front structure, the chassis or the
    # bonnet (276, 197, 232...). Forza mirrors are often mounted on the A-pillar or the
    # sail ahead of the window, which the converted car draws through the chassis
    # (or on a door held shut, DamagePlan.bound). Under the host's door record they
    # swung out with the door whenever a crash knocked it ajar, and floated beside the
    # body (issue #187: Devon GTX, Lancer Evo X). Such a mirror goes under the record
    # that moves its mount as drawn.

    def _mirror_mount(self, pair) -> tuple | None:
        """The panel a mirror panel is mounted on: the one with the most mirror points
        within MOUNT_TOUCH of its own (finest full-entry LOD), else the nearest within
        MOUNT_REACH; None if no panel is that close."""
        pts = np.array(self.boxes.get(pair, ()), dtype=float).reshape(-1, 3)
        if not len(pts):
            return None
        lo, hi = pts.min(0) - MOUNT_REACH, pts.max(0) + MOUNT_REACH
        best = None
        for other, opts in self.boxes.items():
            panel = self.panels.get(other)
            if other == pair or panel is None or panel.role in MIRRORS:
                continue
            q = np.array(opts, dtype=float).reshape(-1, 3)
            q = q[((q >= lo) & (q <= hi)).all(1)]
            if not len(q):
                continue
            d = np.full(len(pts), np.inf)
            for k in range(0, len(q), 2048):
                d = np.minimum(d, np.sqrt(((pts[:, None, :] - q[None, k : k + 2048, :]) ** 2).sum(-1)).min(1))
            if d.min() > MOUNT_REACH:
                continue
            score = (int((d <= MOUNT_TOUCH).sum()), -float(d.min()))
            if best is None or score > best[0]:
                best = (score, other)
        return best[1] if best else None

    def _remount_mirrors(self, data: bytes) -> tuple[bytes, dict]:
        """Put each mirror record under the record whose transform slot draws its
        mount (DamagePlan.bound), where its host parent is another (move_subtree).
        When that record can't take it in slot order, under the chassis' instead: a
        mirror on a bonnet the door record held (ARX-01b), or on a door the bonnet
        record held (DBR9), stays on the body rather than leaving with a panel it
        isn't on."""
        report = {}
        pending = {}
        for pair, panel in sorted(self.panels.items()):
            if panel.role in MIRRORS and pair in self.boxes:
                mount = self._mirror_mount(pair)
                if mount is not None:
                    pending[pair] = mount
        # Both mirrors under one record (the DBR9's bonnet): the one with the higher slot
        # has to leave first, so the rest are tried again until none moves.
        while pending:
            moved = False
            for pair, mount in list(pending.items()):
                out, entry = self._remount(data, pair, mount)
                if out is not None:
                    data, moved = out, True
                    del pending[pair]
                    if entry:
                        report[self.label(pair)] = entry
            if not moved:
                break
        for pair, mount in pending.items():
            report[self.label(pair)] = {"mount": self.label(mount), "kept": "host parent"}
        return data, report

    def _remount(self, data: bytes, pair, mount) -> tuple[bytes | None, dict | None]:
        """One mirror of _remount_mirrors: (the hierarchy, what moved), the hierarchy
        unchanged and None if its parent already moves its mount, (None, None) if it
        can't move in slot order (yet)."""
        slot = self.bound(mount)[0]
        h = upvh_mod.parse(data)
        par = _parents(h)
        mirror = next((r for r in h.records if r.type == 1 and (r.slot, r.model) == pair), None)
        if mirror is None or par[mirror.index] is None:
            return data, None
        parent = h.records[par[mirror.index]]
        # A record without a slot of its own (the front structure) moves with the chassis.
        if (parent.slot if parent.slot >= 0 else self.chassis[0]) == slot:
            return data, None
        chassis = next((r for r in h.records if r.type == 1 and r.role == 38), None)
        target = next((r for r in h.records if r.type == 1 and (r.slot, r.model) == mount), None)
        if target is None or target.slot != slot:
            target = chassis if slot == self.chassis[0] else None
        targets = [target] if target is not None else []
        if chassis is not None and target is not chassis and parent is not chassis:
            targets.append(chassis)
        blockers = self._blockers(h, par, mirror)
        for to in targets:
            for k in range(len(blockers) + 1 if to is chassis else 1):
                out = _move_in_turn(data, blockers[:k][::-1] + [mirror], to)
                if out is not None:
                    entry = {"mount": self.label(mount), "from": role_name(parent.role), "to": role_name(to.role)}
                    if k:
                        entry["with"] = [self.label((max(r.slot, 0), r.model)) for r in blockers[:k]]
                    return out, entry
        return None, None

    # On 22 hosts the door record holds a door-glass record after the mirror (the DB9's
    # host 133: door slot 8 / index 10, mirror 9 / 11, glass index 12), so the mirror
    # alone has no place outside the door that keeps both orders (issue #193). The
    # converted car draws nothing through those glass records (the side windows are on
    # their door's or the chassis' panel, section_rule), so they go under the chassis
    # first, last one first, then the mirror between the door and them. Markers (the
    # door's role 65/67) stay where they are.

    def _blockers(self, h, par, mirror) -> list:
        """The part records after a mirror among its siblings that may leave with it, in
        record order: up to the first that is a mirror, holds a record other than parts
        and markers, or has a part drawn through its own transform slot. They only go
        under the chassis."""
        drawn = set(self.boxes) | self.drawn
        out = []
        c = mirror.index + mirror.subtree
        stop = par[mirror.index] + h.records[par[mirror.index]].subtree
        while c < stop:
            sub = h.records[c : c + h.records[c].subtree]
            c += sub[0].subtree
            if sub[0].type != 1 and not any(r.type == 1 for r in sub):
                continue  # markers, lights, seats: no numbers in the way
            if any(r.type not in (1, 3) or r.role in MIRRORS for r in sub) or any(
                r.type == 1
                and r.slot >= 0
                and (r.slot, r.model) in drawn
                and self.bound((r.slot, r.model))[0] == r.slot
                for r in sub
            ):
                break
            out.append(sub[0])
        return out
