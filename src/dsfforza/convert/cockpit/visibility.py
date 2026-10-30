"""What the driver can see: decimated shells, ray hits, the software rasteriser behind
visible_triangles, glass sight lines and culling."""

from __future__ import annotations

import math

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.geometry import CockpitFit, Wheel, _cross, _dot, _norm, _scale, _sub
from dsfforza.formats import carbin


def decimate_section(section: carbin.Section, cell: float) -> carbin.Section:
    """Vertex clustering: snap LOD0 vertices to a `cell` grid per (subsection
    material, rounded normal), keep one representative each, drop collapsed
    triangles. Crude, but keeps UVs per material."""
    import copy

    if cell <= 0:
        return section
    out = copy.copy(section)
    pool = section.lod0_vertices
    new_pool, subs = [], []
    for sub in section.subsections:
        if sub.lod != 0:
            subs.append(sub)
            continue
        reps, tris = {}, []
        for t in sub.triangles():
            ids = []
            for i in t:
                v = pool[i]
                key = (tuple(round(c / cell) for c in v.position), tuple(round(c * 2) for c in v.normal))
                if key not in reps:
                    reps[key] = len(new_pool)
                    new_pool.append(v)
                ids.append(reps[key])
            if len(set(ids)) == 3:
                tris.append(tuple(ids))
        s2 = copy.copy(sub)
        s2.index_type = 4
        s2.indices = [i for t in tris for i in t]
        subs.append(s2)
    out.lod0_vertices = new_pool
    out.subsections = subs
    return out


CUBE_FACES = [  # (axis, sign): view direction of each cube-map face
    (0, 1),
    (0, -1),
    (1, 1),
    (1, -1),
    (2, 1),
    (2, -1),
]


def _ray_hit(origin, direction, tri) -> float | None:
    """Distance along a unit ray to a triangle (Moller-Trumbore), or None."""
    p0, p1, p2 = tri
    e1, e2 = _sub(p1, p0), _sub(p2, p0)
    h = _cross(direction, e2)
    a = _dot(e1, h)
    if abs(a) < 1e-12:
        return None
    f = 1 / a
    sv = _sub(origin, p0)
    u = f * _dot(sv, h)
    if u < 0 or u > 1:
        return None
    q = _cross(sv, e1)
    v = f * _dot(direction, q)
    if v < 0 or u + v > 1:
        return None
    t = f * _dot(e2, q)
    return t if t > 0 else None


# Rays from the eye (Forza space, front -z) that must reach the windscreen.
# A 3 x 3 fan, 0 to 11 deg up and 17 deg either side: single rays catch the
# cowl, the frit band or a sun strip (black_glass) on cars whose windscreen is open.
WINDSCREEN_RAYS = [_norm((x, y, -1.0)) for x in (-0.3, 0.0, 0.3) for y in (0.0, 0.1, 0.2)]


def shell_blocks_view(shell: list[carbin.Section], lod: int, eye, reach: float = 2.5) -> bool:
    """Does the shell at `lod` close the windscreen (two thirds of the rays
    from the eye stopped within `reach`)? Some main carbins close the window
    openings at their coarsest LOD (FH1 Dodge Durango, Chevrolet Express:
    LOD 5 body)."""
    tris = [
        [s.vertices_for(sub)[i].position for i in t]
        for s in shell
        for sub in s.subsections
        if sub.lod == lod and fm.classify(sub.material)[0] != fm.GLASS
        for t in sub.triangles()
    ]
    stopped = sum(
        1 for ray in WINDSCREEN_RAYS if any((d := _ray_hit(eye, ray, tri)) is not None and d < reach for tri in tris)
    )
    return stopped * 3 >= len(WINDSCREEN_RAYS) * 2


def wheel_targets(wheel: Wheel) -> list[tuple]:
    """The steering wheel's hub and eight points around its rim (Forza space)."""
    a = wheel.axis
    t = _norm(_cross(a, (1.0, 0.0, 0.0)) if abs(a[0]) < 0.9 else _cross(a, (0.0, 1.0, 0.0)))
    b = _cross(a, t)
    rim = [
        tuple(wheel.centre[k] + wheel.radius * (math.cos(th) * t[k] + math.sin(th) * b[k]) for k in range(3))
        for th in (i * math.pi / 4 for i in range(8))
    ]
    return [wheel.centre] + rim


def shell_lids_cockpit(shell: list[carbin.Section], lod: int, eye, wheel: Wheel, margin: float = 0.02) -> int:
    """How many rays from the eye to the steering wheel (wheel_targets) the
    shell at `lod` stops before they reach it, glass included. Open cars' main
    carbins close the cockpit with a lid at their coarsest LOD (Audi R10, R15:
    a flat body panel at the height of the wheel's top), which the
    windscreen rays pass over."""
    targets = wheel_targets(wheel)
    lo = [min(p[k] for p in [eye, *targets]) for k in range(3)]
    hi = [max(p[k] for p in [eye, *targets]) for k in range(3)]
    tris = [
        tri
        for s in shell
        for sub in s.subsections
        if sub.lod == lod
        for t in sub.triangles()
        for tri in [[s.vertices_for(sub)[i].position for i in t]]
        if all(min(p[k] for p in tri) <= hi[k] and max(p[k] for p in tri) >= lo[k] for k in range(3))
    ]
    stopped = 0
    for p in targets:
        d = _sub(p, eye)
        length = math.sqrt(_dot(d, d))
        ray = _scale(d, 1 / length)
        if any((h := _ray_hit(eye, ray, tri)) is not None and h < length - margin for tri in tris):
            stopped += 1
    return stopped


LID_RAYS = 3  # eye-to-wheel rays (of 9) a shell may stop before it counts as a lid


def shell_closes(shell: list[carbin.Section], lod: int, cf: CockpitFit) -> str | None:
    """What the shell at `lod` closes from the eye: "windscreen"
    (shell_blocks_view), "cockpit" (shell_lids_cockpit) or None."""
    if shell_blocks_view(shell, lod, cf.eye):
        return "windscreen"
    if shell_lids_cockpit(shell, lod, cf.eye, cf.wheel) >= LID_RAYS:
        return "cockpit"
    return None


NEAR_PLANE = 0.01  # m, cube-face near plane for the visibility pass
# Head-movement volume for the visibility pass (Forza space, m): DSF's cockpit
# camera bobs, leans and looks around (holes in the IS F and M3 when only
# the Forza eye and 8 cm either side were used, below the raised camera).
CULL_OFFSETS = [
    (0.0, 0.0, 0.0),
    (0.12, 0.0, 0.0),
    (-0.12, 0.0, 0.0),
    (0.0, 0.08, 0.0),
    (0.0, -0.06, 0.0),
    (0.0, 0.0, 0.08),
    (0.0, 0.0, -0.08),
]


def _clip_near(poly: list[tuple], near: float = NEAR_PLANE) -> list[tuple]:
    """Clip a polygon of (u, v, z) points to z >= near (Sutherland-Hodgman, one plane)."""
    out = []
    for i, a in enumerate(poly):
        b = poly[(i + 1) % len(poly)]
        ina, inb = a[2] >= near, b[2] >= near
        if ina:
            out.append(a)
        if ina != inb:
            t = (near - a[2]) / (b[2] - a[2])
            out.append(tuple(a[k] + (b[k] - a[k]) * t for k in range(3)))
    return out


def _face_parts(r, axis: int, sign: int, half: float) -> list[list[tuple]]:
    """A triangle relative to the eye, projected on the cube-map face looking
    along `sign` x `axis`: screen triangles of (x, y, 1 / z), clipped at
    NEAR_PLANE where it crosses it; none if it lies behind."""
    u_axis, v_axis = [a for a in range(3) if a != axis]
    face = [(p[u_axis], p[v_axis], sign * p[axis]) for p in r]
    if max(z for _, _, z in face) < NEAR_PLANE:
        return []
    if min(z for _, _, z in face) < NEAR_PLANE:
        poly = _clip_near(face)
        parts = [(poly[0], poly[i], poly[i + 1]) for i in range(1, len(poly) - 1)]
    else:
        parts = [face]
    return [[(half + half * u / z, half - half * v / z, 1 / z) for u, v, z in part] for part in parts]


def _screen_parts(rel, axis: int, sign: int, half: float, skip=None):
    """_face_parts for many triangles at once: rel (T, 3, 3) eye-relative points.
    Returns (parts (P, 3, 3) of screen (x, y, 1 / z), owner (P,) triangle index),
    ordered by triangle, then part. The same float operations as _face_parts; the
    triangles crossing the near plane are clipped as _clip_near does (_clip_near_many)."""
    import numpy as np

    u_axis, v_axis = [a for a in range(3) if a != axis]
    z = rel[:, :, axis] * sign
    z0, z1, z2 = z[:, 0], z[:, 1], z[:, 2]
    live = np.maximum(np.maximum(z0, z1), z2) >= NEAR_PLANE
    if skip is not None:
        live &= ~skip
    crossing = live & (np.minimum(np.minimum(z0, z1), z2) < NEAR_PLANE)
    whole = np.flatnonzero(live & ~crossing)
    uvz = [np.stack((rel[whole, :, u_axis], rel[whole, :, v_axis], z[whole]), axis=-1)]
    owner = [whole]
    cross = np.flatnonzero(crossing)
    if len(cross):
        face = np.stack((rel[cross, :, u_axis], rel[cross, :, v_axis], z[cross]), axis=-1)
        clipped, which = _clip_near_many(face)
        uvz.append(clipped)
        owner.append(cross[which])
    uvz, owner = np.concatenate(uvz), np.concatenate(owner)
    # By triangle, then part: an owner's parts are consecutive and in order.
    order = np.argsort(owner, kind="stable")
    uvz, owner = uvz[order], owner[order]
    u, v, zz = uvz[..., 0], uvz[..., 1], uvz[..., 2]
    return np.stack((half + half * u / zz, half - half * v / zz, 1 / zz), axis=-1), owner


def _clip_near_many(face):
    """_clip_near and the fan of _face_parts for triangles (C, 3, 3) of (u, v, z)
    that cross the near plane (at least one vertex either side): (parts (P, 3, 3),
    index into `face` (P,)), ordered by triangle, then part. The same float
    operations: per edge the start vertex if inside, then the crossing point
    a + (b - a) * t if the edge crosses; the polygon of 3 or 4 points is fanned
    from its first point."""
    import numpy as np

    count = len(face)
    inside = face[:, :, 2] >= NEAR_PLANE
    slots = np.zeros((count, 6, 3))
    valid = np.zeros((count, 6), dtype=bool)
    for i in range(3):
        a, b = face[:, i], face[:, (i + 1) % 3]
        slots[:, 2 * i] = a
        valid[:, 2 * i] = inside[:, i]
        crosses = np.flatnonzero(inside[:, i] != inside[:, (i + 1) % 3])
        a, b = a[crosses], b[crosses]
        t = (NEAR_PLANE - a[:, 2]) / (b[:, 2] - a[:, 2])
        slots[crosses, 2 * i + 1] = a + (b - a) * t[:, None]
        valid[crosses, 2 * i + 1] = True
    # The valid slots in order: the polygon (3 or 4 points; pad the 3s with their last).
    order = np.argsort(~valid, axis=1, kind="stable")[:, :4]
    poly = np.take_along_axis(slots, order[:, :, None], axis=1)
    parts = np.stack((poly[:, [0, 1, 2]], poly[:, [0, 2, 3]]), axis=1)  # (C, 2, 3, 3)
    has = np.stack((np.ones(count, dtype=bool), valid.sum(axis=1) == 4), axis=1)  # (C, 2)
    which = np.repeat(np.arange(count), 2)[has.ravel()]
    return parts.reshape(-1, 3, 3)[has.ravel()], which


RASTER_TILE = 8  # largest tile side of _raster, in pixels


def _raster(parts, owner, resolution: int, depth, ids=None, chunk: int = 1 << 16):
    """Rasterise screen parts (from _screen_parts) into `depth` (1 / z, larger is
    nearer; a flat float array of resolution**2) and, if given, `ids` (the owner
    of the nearest part), with the per-pixel loop's float operations: a pixel
    centre inside a part (barycentric weights all >= 0) whose w is above the
    pixel's depth takes it, so among equal depths the earlier part keeps it.
    Returns (covered: the part covered a pixel centre, bbox: its pixel box isn't
    empty), one bool per part.

    The loop's result doesn't depend on the order the pixels are visited in: per
    pixel it is the largest w (if above the depth it had), from the earliest part
    with that w. So the parts' pixel boxes are cut into tiles of 1 to RASTER_TILE
    pixels a side (powers of two, the smallest that fit the box), and the tiles of
    one size are evaluated together as (tiles, rows, columns) arrays; the edge
    terms (qx - xc), (ry - yc) ... are computed per column and per row and
    broadcast, the same subtractions the loop does per pixel. Tile pixels outside
    the box are masked off. The largest w per pixel and the earliest part among
    equals are then found with ufunc.at (np.maximum.at, np.minimum.at)."""
    import numpy as np

    px, py, pw = parts[:, 0, 0], parts[:, 0, 1], parts[:, 0, 2]
    qx, qy, qw = parts[:, 1, 0], parts[:, 1, 1], parts[:, 1, 2]
    rx, ry, rw = parts[:, 2, 0], parts[:, 2, 1], parts[:, 2, 2]
    # int() truncates toward zero, as np.trunc does.
    x0 = np.maximum(np.trunc(np.minimum(np.minimum(px, qx), rx)), 0).astype(np.int64)
    x1 = np.minimum(np.trunc(np.maximum(np.maximum(px, qx), rx)) + 1, resolution).astype(np.int64)
    y0 = np.maximum(np.trunc(np.minimum(np.minimum(py, qy), ry)), 0).astype(np.int64)
    y1 = np.minimum(np.trunc(np.maximum(np.maximum(py, qy), ry)) + 1, resolution).astype(np.int64)
    bbox = (x0 < x1) & (y0 < y1)
    area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
    drawn = np.flatnonzero(bbox & ~(np.abs(area) < 1e-12))
    covered = np.zeros(len(parts), dtype=bool)
    if not len(drawn):
        return covered, bbox
    # Tile sides per part: the smallest power of two >= the box side, at most RASTER_TILE.
    sides = [1 << b for b in range(RASTER_TILE.bit_length())]
    width, height = (x1 - x0)[drawn], (y1 - y0)[drawn]
    tw, th = np.ones_like(width), np.ones_like(height)
    for side in sides[1:]:
        tw[width > side // 2] = side
        th[height > side // 2] = side
    nx, ny = -(-width // tw), -(-height // th)
    frags_k, frags_w, frags_n = [], [], []
    for side_w in sides:
        for side_h in sides:
            sel = np.flatnonzero((tw == side_w) & (th == side_h))
            if not len(sel):
                continue
            # One work item per tile: (part, tile column, tile row).
            count = nx[sel] * ny[sel]
            item_part = np.repeat(drawn[sel], count)
            local = np.arange(len(item_part), dtype=np.int64) - np.repeat(np.cumsum(count) - count, count)
            item_nx = np.repeat(nx[sel], count)
            item_x = x0[item_part] + (local % item_nx) * side_w
            item_y = y0[item_part] + (local // item_nx) * side_h
            step = max(1, chunk // (side_w * side_h))
            cols, rows = np.arange(side_w, dtype=np.int64), np.arange(side_h, dtype=np.int64)
            shift_w = side_w.bit_length() - 1
            for start in range(0, len(item_part), step):
                # Arrays are (rows, columns, tiles): the tile axis innermost and contiguous.
                p = item_part[start : start + step]
                x = cols[:, None] + item_x[start : start + step]  # (columns, tiles)
                y = rows[:, None] + item_y[start : start + step]  # (rows, tiles)
                xc, yc = x + 0.5, y + 0.5
                # Tile pixels outside the box: NaN centres, never inside (NaN >= 0 is False).
                xc[x >= x1[p]] = np.nan
                yc[y >= y1[p]] = np.nan
                qx_xc, rx_xc, px_xc = (qx[p] - xc)[None], (rx[p] - xc)[None], (px[p] - xc)[None]
                ry_yc, qy_yc, py_yc = (ry[p] - yc)[:, None], (qy[p] - yc)[:, None], (py[p] - yc)[:, None]
                a = area[p]
                w0 = qx_xc * ry_yc
                w0 -= qy_yc * rx_xc
                w0 /= a
                w1 = rx_xc * py_yc
                w1 -= ry_yc * px_xc
                w1 /= a
                w2 = np.subtract(1, w0)
                w2 -= w1
                inside = w0 >= 0
                inside &= w1 >= 0
                inside &= w2 >= 0
                f = np.flatnonzero(inside)
                if not len(f):
                    continue
                rc, t = np.divmod(f, len(p))  # f = (row * side_w + column) * tiles + tile
                n = p[t]
                covered[n] = True
                w = w0.ravel()[f] * pw[n] + w1.ravel()[f] * qw[n] + w2.ravel()[f] * rw[n]
                keep = w > 0
                rc, t, n = rc[keep], t[keep], n[keep]
                frags_k.append(y[rc >> shift_w, t] * resolution + x[rc & (side_w - 1), t])
                frags_w.append(w[keep])
                frags_n.append(n)
    if not frags_k:
        return covered, bbox
    k, w, n = np.concatenate(frags_k), np.concatenate(frags_w), np.concatenate(frags_n)
    # Per pixel the nearest fragment, the earliest part among equals; it takes the
    # pixel if it is nearer than the depth there.
    nearest = depth.copy()
    np.maximum.at(nearest, k, w)
    won = nearest > depth
    tie = (w == nearest[k]) & won[k]
    first = np.full(len(depth), len(parts), dtype=np.int64)
    np.minimum.at(first, k[tie], n[tie])
    depth[won] = nearest[won]
    if ids is not None:
        ids[won] = owner[first[won]]
    return covered, bbox


def visible_triangles(
    sections: list[carbin.Section],
    eyes: list[tuple],
    resolution: int = 256,
    near_keep: float = 0.25,
    tiny_slack: float = 0.03,
) -> dict[tuple[int, int], set[int]]:
    """Triangles of the LOD-0 subsections that are seen from any of `eyes`:
    an ID buffer per cube-map face (no back-face culling), plus every
    triangle with a vertex within `near_keep` of an eye, plus one ring of
    neighbours (triangles sharing a vertex with a visible one) so slivers
    along visible surfaces stay. Glass (fm role glass) neither occludes nor
    is culled. Triangles crossing a face's near plane are clipped, not
    skipped (large dash and floor triangles next to the head, the IS F's and M3's);
    triangles too small to cover a pixel
    centre count as seen when they are within `tiny_slack` (relative depth)
    of what the pixel shows. Returns {(section index, subsection index):
    kept triangle indices}. Forza space."""
    import numpy as np

    # Per LOD-0 subsection (in order): its key, its triangles' vertex indices into
    # the section's pool and whether it is glass; points and vertex ids per triangle.
    blocks, points, vertex_ids, glass = [], [], [], []
    vertex_base = 0
    for si, s in enumerate(sections):
        pool = None
        for ui, sub in enumerate(s.subsections):
            if sub.lod != 0:
                continue
            if pool is None:
                pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
            t = np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)
            blocks.append(((si, ui), len(t)))
            points.append(pool[t])
            vertex_ids.append(t + vertex_base)
            glass.append(np.full(len(t), fm.classify_cockpit(sub.material)[0] == fm.GLASS))
        vertex_base += len(s.lod0_vertices)
    if not blocks:
        return {}
    points, vertex_ids, glass = np.concatenate(points), np.concatenate(vertex_ids), np.concatenate(glass)
    seen = glass.copy()
    half = resolution / 2
    for eye in eyes:
        rel = points - np.array(eye, dtype=np.float64)
        x, y, z = rel[..., 0], rel[..., 1], rel[..., 2]
        seen[(x * x + y * y + z * z).min(axis=1) < near_keep * near_keep] = True
        for axis, sign in CUBE_FACES:
            ids = np.full(resolution * resolution, -1, dtype=np.int64)
            depth = np.zeros(resolution * resolution)  # 1 / z, larger is nearer
            parts, owner = _screen_parts(rel, axis, sign, half, glass)
            covered, bbox = _raster(parts, owner, resolution, depth, ids)
            seen[ids[ids >= 0]] = True
            # A triangle that covered no pixel centre: the pixel under the centroid of
            # its last part whose pixel box isn't empty, at that part's nearest 1 / z.
            cx = np.trunc((parts[:, 0, 0] + parts[:, 1, 0] + parts[:, 2, 0]) / 3)
            cy = np.trunc((parts[:, 0, 1] + parts[:, 1, 1] + parts[:, 2, 1]) / 3)
            best = bbox & (cx >= 0) & (cx < resolution) & (cy >= 0) & (cy < resolution)
            hit = np.zeros(len(points), dtype=bool)
            hit[owner[covered]] = True
            n = owner[best]  # ascending: parts are ordered by triangle
            k = (cy[best] * resolution + cx[best]).astype(np.int64)
            w = parts[best, :, 2].max(axis=1)
            last = np.ones(len(n), dtype=bool)  # each triangle's last such part
            last[:-1] = n[1:] != n[:-1]
            n, k, w = n[last], k[last], w[last]
            seen[n[~hit[n] & (w >= depth[k] * (1 - tiny_slack))]] = True
    near = np.zeros(vertex_base, dtype=bool)
    near[vertex_ids[seen & ~glass].ravel()] = True
    keep = seen | near[vertex_ids].any(axis=1)
    kept: dict[tuple[int, int], set[int]] = {}
    start = 0
    for key, count in blocks:
        local = np.flatnonzero(keep[start : start + count])
        if len(local):
            kept[key] = set(local.tolist())
        start += count
    return kept


class GlassSight:
    """Which points the driver sees: a depth cube (`resolution` per face) of the
    opaque LOD-0 cockpit triangles from `eye` (Forza space). Called with a DSF
    point (`place` maps Forza to DSF), True if nothing opaque is nearer the
    eye. glass.InteriorGlass starts crack lines and places rim shards where
    the driver can see them; a Forza windscreen runs well below the dash."""

    def __init__(self, sections: list[carbin.Section], eye, place, resolution: int = 128, triangles=None):
        """`triangles` (Forza-space point triples) instead of `sections`: any opaque mesh."""
        self.eye, self.place, self.res = eye, place, resolution
        self.faces = {}
        half = resolution / 2
        import numpy as np

        tris = [np.array([[p[:3] for p in t] for t in triangles or []], dtype=np.float64).reshape(-1, 3, 3)]
        for s in sections:
            pool = None
            for sub in s.subsections:
                if sub.lod != 0 or fm.classify_cockpit(sub.material)[0] == fm.GLASS:
                    continue
                if pool is None:
                    pool = np.array([v.position for v in s.lod0_vertices], dtype=np.float64).reshape(-1, 3)
                tris.append(pool[np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)])
        rel = np.concatenate(tris) - np.array(eye[:3], dtype=np.float64)
        for axis, sign in CUBE_FACES:
            depth = np.zeros(resolution * resolution)  # 1 / z, larger is nearer
            parts, owner = _screen_parts(rel, axis, sign, half)
            _raster(parts, owner, resolution, depth)
            self.faces[(axis, sign)] = depth.tolist()

    def __call__(self, point_dsf, slack: float = 0.03) -> bool:
        # DSF -> Forza: Placement.point is (-x, y + dy, -z + dz), its own inverse up to the offsets
        p = (-point_dsf[0], point_dsf[1] - self.place.dy, self.place.dz - point_dsf[2])
        r = [p[a] - self.eye[a] for a in range(3)]
        axis = max(range(3), key=lambda a: abs(r[a]))
        sign = 1 if r[axis] > 0 else -1
        u_axis, v_axis = [a for a in range(3) if a != axis]
        z = sign * r[axis]
        if z < NEAR_PLANE:
            return True
        half = self.res / 2
        x, y = int(half + half * r[u_axis] / z), int(half - half * r[v_axis] / z)
        if not (0 <= x < self.res and 0 <= y < self.res):
            return True
        return 1 / z >= self.faces[(axis, sign)][y * self.res + x] * (1 - slack)


def cull_sections(sections: list[carbin.Section], kept: dict[tuple[int, int], set[int]]) -> list[carbin.Section]:
    """Copies of `sections` whose LOD-0 subsections hold only the kept triangles."""
    import copy

    out = []
    for si, s in enumerate(sections):
        s2 = copy.copy(s)
        subs = []
        for ui, sub in enumerate(s.subsections):
            if sub.lod != 0:
                subs.append(sub)
                continue
            keep = kept.get((si, ui), set())
            tris = [t for k, t in enumerate(sub.triangles()) if k in keep]
            if not tris:
                continue
            sub2 = copy.copy(sub)
            sub2.index_type = 4
            sub2.indices = [i for t in tris for i in t]
            subs.append(sub2)
        s2.subsections = subs
        out.append(s2)
    return out
