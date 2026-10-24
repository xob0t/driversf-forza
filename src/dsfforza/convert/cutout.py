"""Alpha-tested decals (badges, emblems) cut to their alpha mask as geometry.

Forza draws its badge techniques with an alpha test: texels with alpha below one
half are discarded, so a badge is often one quad over the lettering, the
letters in the alpha channel and one flat colour in RGB (the 205 T16's
"205 turbo 16": a quad of solid red). DSF's Chassis material, which converted
badges draw with, has no alpha test, so the whole quad showed: a red bar on the
205's tailgate, cream plates on the Firebird, a dark band across the Mustang
Boss 429's bonnet stripe (research/forza-materials.md, "Badge plates").
`dsf_materials.Texturing` drops triangles the test removes entirely
(`forza_materials.triangle_alpha_cut`); this module replaces the triangles it
removes in part with triangles covering only the texels that pass:

- the texel centres inside the UV triangle with alpha >= 128 form a mask;
- its outlines (marching squares at the half-way line between texel centres)
  are simplified (Douglas-Peucker, CUT_TOLERANCE texels) and triangulated
  (ear clipping, holes bridged to their outline); an outline that does not
  triangulate falls back to the mask's rectangles (exact, more triangles);
- each new vertex is the source triangle's barycentric blend at that point
  (position, UVs, normal, Forza AO byte), so the pieces stay on the surface and
  sample the same texels as before.

    cut_alpha_tested(sections, atlases) -> (sections, report)
"""

from __future__ import annotations

import dataclasses
import math

from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin

# A kept alpha-tested triangle is cut when the test hides at least this share of
# the texel centres inside it (and it covers at least CUT_MIN_TEXELS). Below it
# only soft emblem edges are hidden (the 40 testdrive cars' modelled emblems: 0
# to 15% at LOD 1), badges drawn as plain quads hide 35 to 90%.
CUT_MIN_HIDDEN = 0.2
CUT_MIN_TEXELS = 8
CUT_TOLERANCE = 0.75  # texels (source atlas): outline simplification at LOD 0 to 2,
# x (LOD - 1) beyond (LOD 3: 1.5, LOD 5: 3; the full entry's far LODs)
MASK_MARGIN = 2.0  # texels of the mask kept beyond a triangle's edges before clipping
ALPHA_PASS = 128


# --- mask -> polygons ----------------------------------------------------------------


def _loops(mask) -> list[list[tuple[float, float]]]:
    """Closed outlines of a boolean grid (mask[y][x], sampled at texel centres
    x + 0.5, y + 0.5), counter-clockwise around filled areas (y down: as seen in
    the texture), clockwise around holes. Marching squares with the outline
    through the midpoints between samples; saddles join the filled corners."""
    h = len(mask)
    w = len(mask[0]) if h else 0

    def at(x, y):
        return 0 <= x < w and 0 <= y < h and bool(mask[y][x])

    # Segment keys: doubled coordinates of edge midpoints between sample points.
    nxt: dict = {}
    for y in range(-1, h):
        for x in range(-1, w):
            a, b, c, d = at(x, y), at(x + 1, y), at(x + 1, y + 1), at(x, y + 1)  # tl tr br bl
            if a == b == c == d:
                continue
            top, right = (2 * x + 1, 2 * y), (2 * x + 2, 2 * y + 1)
            bottom, left = (2 * x + 1, 2 * y + 2), (2 * x, 2 * y + 1)
            # Each crossing as (from, to) with the filled side on the walker's right as
            # seen in the texture (x right, y down); saddles join the filled corners.
            code = a * 8 + b * 4 + c * 2 + d
            table = {
                1: [(left, bottom)],
                2: [(bottom, right)],
                3: [(left, right)],
                4: [(right, top)],
                5: [(left, top), (right, bottom)],  # saddle: tr and bl filled
                6: [(bottom, top)],
                7: [(left, top)],
                8: [(top, left)],
                9: [(top, bottom)],
                10: [(top, right), (bottom, left)],  # saddle: tl and br filled
                11: [(top, right)],
                12: [(right, left)],
                13: [(right, bottom)],
                14: [(bottom, left)],
            }
            nxt.update(table[code])
    loops = []
    while nxt:
        start, cur = next(iter(nxt.items()))
        del nxt[start]
        loop = [start]
        while cur != start:
            loop.append(cur)
            cur = nxt.pop(cur, None)
            if cur is None:  # open chain: should not happen on a padded grid
                loop = []
                break
        if len(loop) >= 3:
            # midpoint keys -> texel-space coordinates (sample (x, y) sits at x + 0.5, y + 0.5)
            loops.append([(k[0] / 2 + 0.5, k[1] / 2 + 0.5) for k in reversed(loop)])
    return loops


def _area(poly) -> float:
    return 0.5 * sum(poly[i - 1][0] * poly[i][1] - poly[i][0] * poly[i - 1][1] for i in range(len(poly)))


def _simplify(poly, tolerance: float):
    """Douglas-Peucker on a closed loop (split at its two farthest-apart points)."""
    n = len(poly)
    if n <= 4:
        return poly
    i0 = 0
    i1 = max(range(n), key=lambda i: (poly[i][0] - poly[0][0]) ** 2 + (poly[i][1] - poly[0][1]) ** 2)

    def dp(pts):
        if len(pts) < 3:
            return pts
        (ax, ay), (bx, by) = pts[0], pts[-1]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        best, k = -1.0, 0
        for j in range(1, len(pts) - 1):
            px, py = pts[j]
            d = abs(dx * (ay - py) - dy * (ax - px)) / length if length > 1e-12 else math.hypot(px - ax, py - ay)
            if d > best:
                best, k = d, j
        if best <= tolerance:
            return [pts[0], pts[-1]]
        return dp(pts[: k + 1])[:-1] + dp(pts[k:])

    first = dp(poly[i0 : i1 + 1])
    second = dp(poly[i1:] + [poly[0]])
    out = first[:-1] + second[:-1]
    return out if len(out) >= 3 and abs(_area(out)) > 1e-9 else poly


def _inside(pt, poly) -> bool:
    x, y = pt
    hit = False
    for i in range(len(poly)):
        (x0, y0), (x1, y1) = poly[i - 1], poly[i]
        if (y0 > y) != (y1 > y) and x < x0 + (y - y0) * (x1 - x0) / (y1 - y0):
            hit = not hit
    return hit


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _segments_cross(p1, p2, q1, q2) -> bool:
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return ((d1 > 1e-12 and d2 < -1e-12) or (d1 < -1e-12 and d2 > 1e-12)) and (
        (d3 > 1e-12 and d4 < -1e-12) or (d3 < -1e-12 and d4 > 1e-12)
    )


def _bridge(outer, hole, others=()):
    """Join a hole (clockwise) into its outline (counter-clockwise) through the
    shortest segment from the hole's rightmost vertex to an outline vertex that
    crosses no edge of the outline, the hole or the outline's other holes."""
    hi = max(range(len(hole)), key=lambda i: (hole[i][0], -hole[i][1]))
    m = hole[hi]
    edges = [(q[i - 1], q[i]) for q in (outer, hole, *others) for i in range(len(q))]
    order = sorted(range(len(outer)), key=lambda i: (outer[i][0] - m[0]) ** 2 + (outer[i][1] - m[1]) ** 2)
    for oi in order:
        p = outer[oi]
        if not any(_segments_cross(m, p, a, b) for a, b in edges if p not in (a, b) and m not in (a, b)):
            return outer[: oi + 1] + hole[hi:] + hole[: hi + 1] + outer[oi:]
    return None


def _point_in_tri(p, a, b, c) -> bool:
    """Inside or on the boundary."""
    return _cross(a, b, p) >= -1e-12 and _cross(b, c, p) >= -1e-12 and _cross(c, a, p) >= -1e-12


def _drop_collinear(poly):
    """The loop without vertices on a straight line between their neighbours."""
    out = list(poly)
    changed = True
    while changed and len(out) > 3:
        changed = False
        for k in range(len(out)):
            if abs(_cross(out[k - 1], out[k], out[(k + 1) % len(out)])) <= 1e-12:
                out.pop(k)
                changed = True
                break
    return out


def _ear_clip(poly):
    """Triangles of a simple counter-clockwise polygon (y up), or None."""
    idx = list(range(len(poly)))
    tris = []
    guard = 0
    while len(idx) > 3:
        guard += 1
        if guard > 4 * len(poly) * len(poly):
            return None
        found = False
        n = len(idx)
        for k in range(n):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % n]
            a, b, c = poly[i0], poly[i1], poly[i2]
            if _cross(a, b, c) <= 1e-12:
                continue  # reflex or flat
            if any(_point_in_tri(poly[j], a, b, c) and poly[j] not in (a, b, c) for j in idx if j not in (i0, i1, i2)):
                continue
            tris.append((a, b, c))
            idx.pop(k)
            found = True
            break
        if not found:
            # Only flat corners left (collinear runs from the bridge): drop one.
            flat = [k for k in range(n) if abs(_cross(poly[idx[k - 1]], poly[idx[k]], poly[idx[(k + 1) % n]])) <= 1e-12]
            if not flat:
                return None
            idx.pop(flat[0])
    if len(idx) == 3 and _cross(poly[idx[0]], poly[idx[1]], poly[idx[2]]) > 1e-12:
        tris.append(tuple(poly[i] for i in idx))
    return tris


def _rectangles(mask, x_off: float, y_off: float):
    """Fallback: the mask's row runs merged downwards, two triangles each (texel squares)."""
    h = len(mask)
    tris, open_ = [], {}
    for y in range(h + 1):
        row = mask[y] if y < h else []
        runs, x = set(), 0
        while x < len(row):
            if row[x]:
                x1 = x
                while x1 < len(row) and row[x1]:
                    x1 += 1
                runs.add((x, x1))
                x = x1
            else:
                x += 1
        for run, y0 in list(open_.items()):
            if run not in runs:
                (a, b) = run
                p = [(a + x_off, y0 + y_off), (b + x_off, y0 + y_off), (b + x_off, y + y_off), (a + x_off, y + y_off)]
                tris += [(p[0], p[1], p[2]), (p[0], p[2], p[3])]
                del open_[run]
        for run in runs:
            open_.setdefault(run, y)
    return tris


def mask_triangles(mask, x_off: float = 0.0, y_off: float = 0.0, tolerance: float = CUT_TOLERANCE):
    """Triangles (texel-space points, offset by (x_off, y_off)) covering a
    boolean mask sampled at texel centres; the outlines run half-way between
    filled and empty samples. Outlines that cross themselves once simplified are
    tried again with less simplification (none last: marching-squares loops are
    simple), then the mask's rectangles. Returns (triangles, used_fallback)."""
    # The loops are in texture space (y down); work in y-up (negate y) so that
    # filled outlines are counter-clockwise.
    flipped = [[(x, -y) for x, y in loop] for loop in _loops(mask)]
    for tol in (tolerance, tolerance / 2, 0.0):
        tris = _triangulate([_drop_collinear(_simplify(loop, tol) if tol else loop) for loop in flipped])
        if tris is not None:
            return [tuple((x + x_off, -y + y_off) for x, y in t) for t in tris], False
    return _rectangles(mask, x_off, y_off), True


def _triangulate(simple):
    """Ear-clipped triangles of outlines (y up: filled counter-clockwise, holes
    clockwise), or None if one doesn't triangulate to its area."""
    outers = [p for p in simple if _area(p) > 0]
    holes = [p for p in simple if _area(p) < 0]
    groups = {i: [] for i in range(len(outers))}
    for hole in holes:
        probe = hole[0]
        owners = [i for i, o in enumerate(outers) if _inside((probe[0] + 1e-4, probe[1] + 1e-4), o)]
        if owners:
            groups[min(owners, key=lambda i: abs(_area(outers[i])))].append(hole)
    tris = []
    for i, outer in enumerate(outers):
        poly = outer
        ok = True
        pending = sorted(groups[i], key=lambda q: -max(p[0] for p in q))
        for n, hole in enumerate(pending):
            poly = _bridge(poly, hole, pending[n + 1 :])
            if poly is None:
                ok = False
                break
        part = _ear_clip(poly) if ok else None
        want = _area(outer) + sum(_area(q) for q in groups[i])
        if part is None or abs(sum(_cross(*t) for t in part) / 2 - want) > 0.01 * abs(want) + 0.5:
            return None  # self-crossing after simplification
        tris += part
    return tris


# --- carbin sections ------------------------------------------------------------------


def _tri_mask(alpha, size, pts):
    """(mask rows, x0, y0, inside count, passed count) for a texel-space
    triangle: the counts over the texel centres inside it; the mask holds the
    passing texels within MASK_MARGIN texels of it (clip_to_triangle trims the
    pieces back to its edges, so the pieces of two neighbouring triangles meet
    on their shared edge), padded by one empty texel on every side."""
    width, height = size
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    m = MASK_MARGIN + 1
    x0, x1 = max(0, int(math.floor(min(xs) - m))), min(width, int(math.ceil(max(xs) + m)) + 1)
    y0, y1 = max(0, int(math.floor(min(ys) - m))), min(height, int(math.ceil(max(ys) + m)) + 1)
    (ax, ay), (bx, by), (cx, cy) = pts
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if x0 >= x1 or y0 >= y1 or abs(area) < 1e-9:
        return None
    import numpy as np

    gy, gx = np.mgrid[y0:y1, x0:x1] + 0.5
    w0 = ((bx - gx) * (cy - gy) - (by - gy) * (cx - gx)) / area
    w1 = ((cx - gx) * (ay - gy) - (cy - gy) * (ax - gx)) / area
    w2 = 1 - w0 - w1
    inside = (w0 >= 0) & (w1 >= 0) & (w2 >= 0)
    # Distance (texels) to each edge line: barycentric weight x the height over that edge.
    lengths = [math.hypot(bx - cx, by - cy), math.hypot(cx - ax, cy - ay), math.hypot(ax - bx, ay - by)]
    near = np.ones(w0.shape, bool)
    for w, length in zip((w0, w1, w2), lengths):
        near &= w * abs(area) / max(length, 1e-9) >= -MASK_MARGIN
    passing = alpha[y0:y1, x0:x1] >= ALPHA_PASS
    padded = np.zeros((passing.shape[0] + 2, passing.shape[1] + 2), bool)
    padded[1:-1, 1:-1] = passing & near
    return padded.tolist(), x0 - 1, y0 - 1, int(inside.sum()), int((inside & passing).sum())


def clip_to_triangle(piece, tri):
    """A convex polygon (texel space) clipped to a triangle (Sutherland-Hodgman);
    [] if nothing is left."""
    sign = 1.0 if _cross(*tri) > 0 else -1.0
    poly = list(piece)
    for k in range(3):
        a, b = tri[k], tri[(k + 1) % 3]
        out = []
        for i in range(len(poly)):
            p, q = poly[i - 1], poly[i]
            dp, dq = sign * _cross(a, b, p), sign * _cross(a, b, q)
            if dq >= 0:
                if dp < 0:
                    t = dp / (dp - dq)
                    out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
                out.append(q)
            elif dp >= 0:
                t = dp / (dp - dq)
                out.append((p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1])))
        poly = out
        if len(poly) < 3:
            return []
    return poly


def _blend(corners, pts, point) -> carbin.Vertex:
    """The source triangle's vertex at texel-space `point` (barycentric; clamped
    onto the triangle against rounding)."""
    (ax, ay), (bx, by), (cx, cy) = pts
    px, py = point
    area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    wb = ((px - ax) * (cy - ay) - (py - ay) * (cx - ax)) / area
    wc = ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / area
    w = [max(0.0, 1 - wb - wc), max(0.0, wb), max(0.0, wc)]
    s = sum(w) or 1.0
    w = [k / s for k in w]

    def mix(get, n):
        return tuple(sum(w[k] * get(corners[k])[j] for k in range(3)) for j in range(n))

    normal = mix(lambda v: v.normal, 3)
    length = math.sqrt(sum(c * c for c in normal)) or 1.0
    nearest = corners[max(range(3), key=lambda k: w[k])]
    extra = nearest.extra
    if extra:
        extra = bytes([min(255, round(sum(w[k] * corners[k].extra[0] for k in range(3))))]) + extra[1:]
    return carbin.Vertex(
        mix(lambda v: v.position, 3),
        mix(lambda v: v.uv0, 2),
        mix(lambda v: v.uv1, 2),
        tuple(c / length for c in normal),
        extra,
    )


def cut_alpha_tested(sections, atlases: fm.AtlasCache, lods=None, classifier=fm.classify):
    """Sections with every kept alpha-tested triangle whose texels the alpha test
    hides in part (CUT_MIN_HIDDEN) replaced by triangles over the texels that pass.
    Unchanged sections are returned as they are. lods: only subsections at these
    LODs (None: all). Report: per section/technique, triangles cut, triangles
    made, fallbacks, and the source area removed (m2)."""
    out, report = [], {}
    for section in sections:
        subs, pools, changed = [], {}, False
        for sub in section.subsections:
            if sub.material not in fm.ALPHA_TESTED or (lods is not None and sub.lod not in lods):
                subs.append(sub)
                continue
            m = fm.resolve(section, sub, classifier)
            if m.kind != "textured" or m.family is None or m.role == fm.SKIP:
                subs.append(sub)
                continue
            try:
                width, height, rgba = atlases.get(m.family)
            except (FileNotFoundError, OSError):
                subs.append(sub)
                continue
            import numpy as np

            alpha = np.frombuffer(rgba, dtype=np.uint8)[3::4].reshape(height, width)
            lod0 = sub.lod == 0
            if lod0 not in pools:
                pools[lod0] = list(section.vertices_for(sub))
            pool = pools[lod0]
            source = section.vertices_for(sub)
            indices, cut = [], 0
            stats = report.setdefault(
                f"{section.name}/{sub.material}", {"cut": 0, "made": 0, "fallback": 0, "area": 0.0}
            )
            for tri in sub.triangles():
                corners = [source[i] for i in tri]
                pts = [((u := m.uv(v.uv0))[0] * width, u[1] * height) for v in corners]
                found = _tri_mask(alpha, (width, height), pts)
                if found is None:
                    indices += list(tri)
                    continue
                mask, x0, y0, inside, passed = found
                if inside < CUT_MIN_TEXELS or passed == 0 or inside - passed < CUT_MIN_HIDDEN * inside:
                    indices += list(tri)  # all passes (or too small to judge), or Texturing drops it
                    continue
                pieces, fallback = mask_triangles(mask, x0, y0, CUT_TOLERANCE * max(1, sub.lod - 1))
                cut += 1
                stats["cut"] += 1
                stats["made"] += len(pieces)
                stats["fallback"] += int(fallback)
                p = [c.position for c in corners]
                e1 = [p[1][k] - p[0][k] for k in range(3)]
                e2 = [p[2][k] - p[0][k] for k in range(3)]
                n = (e1[1] * e2[2] - e1[2] * e2[1], e1[2] * e2[0] - e1[0] * e2[2], e1[0] * e2[1] - e1[1] * e2[0])
                stats["area"] += 0.5 * math.sqrt(sum(c * c for c in n)) * (inside - passed) / inside
                # Keep the source triangle's winding: texture-space orientation of the
                # source against that of each piece.
                (ax, ay), (bx, by), (qx, qy) = pts
                flip_src = (bx - ax) * (qy - ay) - (by - ay) * (qx - ax) < 0
                made = {}
                clipped = []
                for piece in pieces:
                    poly = clip_to_triangle(piece, pts)
                    clipped += [(poly[0], poly[k], poly[k + 1]) for k in range(1, len(poly) - 1)]
                stats["made"] += len(clipped) - len(pieces)
                for piece in clipped:
                    (px, py), (rx, ry), (sx, sy) = piece
                    if abs((rx - px) * (sy - py) - (ry - py) * (sx - px)) < 1e-9:
                        stats["made"] -= 1
                        continue  # a sliver along an edge
                    flip = (rx - px) * (sy - py) - (ry - py) * (sx - px) < 0
                    order = piece if flip == flip_src else (piece[0], piece[2], piece[1])
                    for point in order:
                        key = (round(point[0], 4), round(point[1], 4))
                        if key not in made:
                            made[key] = len(pool)
                            pool.append(_blend(corners, pts, point))
                        indices.append(made[key])
            if cut:
                changed = True
                subs.append(dataclasses.replace(sub, index_type=4, indices=indices))
            else:
                report.pop(f"{section.name}/{sub.material}", None)
                subs.append(sub)
        if not changed:
            out.append(section)
            continue
        out.append(
            dataclasses.replace(
                section,
                subsections=subs,
                lod_vertices=pools.get(False, section.lod_vertices),
                lod0_vertices=pools.get(True, section.lod0_vertices),
            )
        )
    return out, report
