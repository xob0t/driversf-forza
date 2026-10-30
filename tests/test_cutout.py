"""cutout's marching squares and ear clipping give what the original loops gave (kept
here as references) on random masks and blobs.

    uv run python tests/test_cutout.py      # or every test: uv run python -m tests
"""

from __future__ import annotations

import random

from dsfforza.convert import cutout


def _loops_reference(mask):
    """The original per-cell marching squares."""
    h = len(mask)
    w = len(mask[0]) if h else 0

    def at(x, y):
        return 0 <= x < w and 0 <= y < h and bool(mask[y][x])

    nxt = {}
    for y in range(-1, h):
        for x in range(-1, w):
            a, b, c, d = at(x, y), at(x + 1, y), at(x + 1, y + 1), at(x, y + 1)
            if a == b == c == d:
                continue
            top, right = (2 * x + 1, 2 * y), (2 * x + 2, 2 * y + 1)
            bottom, left = (2 * x + 1, 2 * y + 2), (2 * x, 2 * y + 1)
            code = a * 8 + b * 4 + c * 2 + d
            table = {
                1: [(left, bottom)],
                2: [(bottom, right)],
                3: [(left, right)],
                4: [(right, top)],
                5: [(left, top), (right, bottom)],
                6: [(bottom, top)],
                7: [(left, top)],
                8: [(top, left)],
                9: [(top, bottom)],
                10: [(top, right), (bottom, left)],
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
            if cur is None:
                loop = []
                break
        if len(loop) >= 3:
            loops.append([(k[0] / 2 + 0.5, k[1] / 2 + 0.5) for k in reversed(loop)])
    return loops


def _ear_clip_reference(poly):
    """The original ear clipping, with _point_in_tri per vertex."""
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
            if cutout._cross(a, b, c) <= 1e-12:
                continue
            if any(
                cutout._point_in_tri(poly[j], a, b, c) and poly[j] not in (a, b, c)
                for j in idx
                if j not in (i0, i1, i2)
            ):
                continue
            tris.append((a, b, c))
            idx.pop(k)
            found = True
            break
        if not found:
            flat = [
                k
                for k in range(n)
                if abs(cutout._cross(poly[idx[k - 1]], poly[idx[k]], poly[idx[(k + 1) % n]])) <= 1e-12
            ]
            if not flat:
                return None
            idx.pop(flat[0])
    if len(idx) == 3 and cutout._cross(poly[idx[0]], poly[idx[1]], poly[idx[2]]) > 1e-12:
        tris.append(tuple(poly[i] for i in idx))
    return tris


def _masks():
    rng = random.Random(7)
    for trial in range(150):
        h, w = rng.randint(0, 30), rng.randint(1, 40)
        if trial % 3 == 0 and h > 4:  # a blob with specks
            cx, cy, r = rng.uniform(0, w), rng.uniform(0, h), rng.uniform(2, 12)
            yield [[(x - cx) ** 2 + (y - cy) ** 2 < r * r or rng.random() < 0.05 for x in range(w)] for y in range(h)]
        else:
            density = rng.random()
            yield [[rng.random() < density for _ in range(w)] for _ in range(h)]
    yield []
    yield [[True]]


def test_loops_and_ear_clipping_match_the_original_loops():
    for mask in _masks():
        loops = cutout._loops(mask)
        assert loops == _loops_reference(mask)
        for loop in loops:
            flipped = [(x, -y) for x, y in loop]
            for tolerance in (0.75, 0.0):
                poly = cutout._drop_collinear(cutout._simplify(flipped, tolerance) if tolerance else flipped)
                assert cutout._ear_clip(poly) == _ear_clip_reference(poly)


def _masks_with_holes():
    rng = random.Random(3)
    for _ in range(120):
        h, w = rng.randint(8, 60), rng.randint(8, 60)
        cx, cy = rng.uniform(0, w), rng.uniform(0, h)
        r = rng.uniform(3, 30)
        holes = [(rng.uniform(0, w), rng.uniform(0, h), rng.uniform(0.5, 6)) for _ in range(rng.randint(1, 12))]
        yield [
            [
                (
                    (x - cx) ** 2 + (y - cy) ** 2 < r * r
                    and not any((x - hx) ** 2 + (y - hy) ** 2 < hr * hr for hx, hy, hr in holes)
                )
                or rng.random() < 0.02
                for x in range(w)
            ]
            for y in range(h)
        ]


def test_mask_triangles_with_holes_match_the_original_ear_clipping():
    # Bridged outlines (holes joined in) are where ears are blocked by far parts of the outline.
    bridged = 0
    for mask in _masks_with_holes():
        got = cutout.mask_triangles(mask, 0.5, -2.0)
        ears = cutout._ear_clip
        cutout._ear_clip = _ear_clip_reference
        try:
            want = cutout.mask_triangles(mask, 0.5, -2.0)
        finally:
            cutout._ear_clip = ears
        assert got == want
        bridged += sum(1 for loop in cutout._loops(mask) if cutout._area([(x, -y) for x, y in loop]) < 0)
    assert bridged > 60, bridged


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
