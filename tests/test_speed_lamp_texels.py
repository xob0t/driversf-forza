"""lenses._texels_many gives exactly what lenses._texels gives triangle by triangle, on random
UV triangles of every kind (inside, across and outside the atlas, slivers, degenerate).

uv run python tests/test_speed_lamp_texels.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import numpy as np

from dsfforza.convert import lenses


def _random_triangles(rng, n, width, height):
    kind = rng.integers(0, 8, n)
    a = rng.uniform(-0.3, 1.3, (n, 2)) * (width, height)
    size = np.where(kind[:, None] < 2, 0.3, np.where(kind[:, None] < 5, 3.0, 30.0))
    pts = a[:, None] + rng.normal(0, 1, (n, 3, 2)) * size[:, :, None]
    pts[kind == 5, 2] = pts[kind == 5, 0]  # degenerate
    pts[kind == 6] = np.round(pts[kind == 6])  # corners on texel corners: centres on the edges
    pts[kind == 7, :, 1] = pts[kind == 7, :1, 1] + 1e-11 * rng.normal(size=(int((kind == 7).sum()), 3))
    return pts


def test_texels_many_matches_texels():
    rng = np.random.default_rng(4)
    for case in range(10):
        width, height = int(rng.choice([4, 64, 512])), int(rng.choice([4, 64, 256]))
        pts = _random_triangles(rng, int(rng.choice([0, 1, 2, 500, 3000])), width, height)
        if case == 2:
            lenses.TEXELS_CHUNK, chunk = 40, lenses.TEXELS_CHUNK  # many chunks, triangles bigger than one
        # big boxes one by one (the default), every box one by one, none
        lenses.TEXELS_ONE_BY_ONE, one_by_one = (256, 0, 10**9)[case % 3], lenses.TEXELS_ONE_BY_ONE
        # and with any number of small boxes batched (the default needs TEXELS_BATCH_MIN)
        lenses.TEXELS_BATCH_MIN, batch_min = (8, 0, 1)[case % 3], lenses.TEXELS_BATCH_MIN
        ix, iy, bary, at = lenses._texels_many(pts)
        lenses.TEXELS_ONE_BY_ONE, lenses.TEXELS_BATCH_MIN = one_by_one, batch_min
        if case == 2:
            lenses.TEXELS_CHUNK = chunk
        assert len(at) == len(pts) + 1 and at[0] == 0 and at[-1] == len(ix) == len(iy) == len(bary)
        for k, tri in enumerate(pts):
            want = lenses._texels(tri.copy())
            got = (ix[at[k] : at[k + 1]], iy[at[k] : at[k + 1]], bary[at[k] : at[k + 1]])
            for g, w in zip(got, want, strict=True):
                assert g.dtype == w.dtype and np.array_equal(g, w), (case, k, tri)


def test_texels_many_non_finite_raises_as_texels():
    pts = np.array([[[0.0, 0.0], [4.0, 0.0], [0.0, 4.0]], [[np.nan, 0.0], [1.0, 1.0], [0.0, 2.0]]])
    for fn in (lenses._texels_many, lambda p: [lenses._texels(t) for t in p]):
        try:
            fn(pts)
        except ValueError:
            continue
        raise AssertionError("no error")


def _dsf_boxes_reference(t, m, sub, k, uvs, out):
    """The original Texturing._dsf_boxes, one triangle."""
    import math

    from dsfforza.convert import dsf_materials as dm

    n = t.owners.get((id(sub), k))
    if n is None:
        return
    r = t.regions[m.family][n]
    s, src_size = t._scale_of(r), t.src_sizes[m.family]
    sh, sw = out.shape
    width, height = t.size
    pts = np.array([r.map(min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), src_size, s, t.size) for u, v in uvs]) * (
        width,
        height,
    )
    ix, iy, _ = lenses._texels(pts)
    if len(ix) <= 4:
        cx, cy = (int(math.floor(c - 0.5)) for c in pts.mean(0))
        ix = np.concatenate([ix, [cx, cx + 1, cx, cx + 1]])
        iy = np.concatenate([iy, [cy, cy, cy + 1, cy + 1]])
    dx, dy = ix - r.dst[0], iy - r.dst[1]
    ok = (dx >= 0) & (dx < r.size[0]) & (dy >= 0) & (dy < r.size[1])
    fx, fy = r.src[0] + (dx[ok] - dm.PAD) / s, r.src[1] + (dy[ok] - dm.PAD) / s
    x0 = np.clip(np.floor(fx).astype(int), 0, sw - 1)
    y0 = np.clip(np.floor(fy).astype(int), 0, sh - 1)
    x1 = np.maximum(x0 + 1, np.minimum(sw, np.floor(fx + 1 / s).astype(int)))
    y1 = np.maximum(y0 + 1, np.minimum(sh, np.floor(fy + 1 / s).astype(int)))
    for oy in range(int((y1 - y0).max(initial=0))):
        for ox in range(int((x1 - x0).max(initial=0))):
            sel = (y0 + oy < y1) & (x0 + ox < x1)
            out[y0[sel] + oy, x0[sel] + ox] = True


def test_dsf_boxes_all_at_once():
    from pathlib import Path

    from dsfforza.convert import dsf_materials as dm
    from dsfforza.convert import forza_materials as fm
    from dsfforza.formats import carbin

    rng = np.random.default_rng(8)
    marked = 0
    for case in range(20):
        sw, sh = int(rng.choice([64, 256])), int(rng.choice([64, 128]))
        t = dm.Texturing(Path(), size=(256, 256))
        t.src_sizes = {fm.LIGHTS: (sw, sh)}
        regions = []
        for _ in range(4):
            x0, y0 = int(rng.integers(0, sw - 8)), int(rng.integers(0, sh - 8))
            src = (x0, y0, int(rng.integers(x0 + 4, sw)), int(rng.integers(y0 + 4, sh)))
            scale = float(rng.choice([0.25, 0.479, 0.5, 1.0, 1.7]))
            size = (int((src[2] - src[0]) * scale) + 2 * dm.PAD, int((src[3] - src[1]) * scale) + 2 * dm.PAD)
            regions.append(
                dm.Region(fm.LIGHTS, src, (int(rng.integers(0, 128)), int(rng.integers(0, 128))), size, scale)
            )
        t.regions = {fm.LIGHTS: regions}
        sub = carbin.SubSection("lights", 1, 4, (0, 1, 0, 1, 0, 1, 0, 1), [])
        ks = list(range(int(rng.integers(0, 60))))
        uvs = []
        for k in ks:
            n = int(rng.integers(0, 5))
            if n < 4:
                t.owners[(id(sub), k)] = n
                r = regions[n]
                c = (rng.uniform(r.src[0], r.src[2]) / sw, rng.uniform(r.src[1], r.src[3]) / sh)
            else:
                c = tuple(rng.uniform(0, 1, 2))  # no region: skipped
            spread = float(rng.choice([0.0005, 0.005, 0.05]))
            uvs.append([tuple(float(x) for x in np.array(c) + rng.normal(0, spread, 2)) for _ in range(3)])
        m = fm.SubMaterial("lights", fm.TEXTURED, fm.LIGHTS, "textured", (0, 1, 0, 1), (0, 0, 1, 1))
        got, want = np.zeros((sh, sw), bool), np.zeros((sh, sw), bool)
        t._dsf_boxes(m, sub, ks, uvs, got)
        for k, tri in zip(ks, uvs):
            _dsf_boxes_reference(t, m, sub, k, tri, want)
        assert np.array_equal(got, want), case
        marked += int(want.sum())
    assert marked > 1000, marked


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
