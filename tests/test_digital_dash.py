"""Checks for src/dsfforza/convert/digital_dash.py and formats/anark.py that need no game files.

uv run python tests/test_digital_dash.py     # or every test: uv run python -m tests digital_dash
"""

from __future__ import annotations

import itertools

import numpy as np

from dsfforza.convert import digital_dash as dd
from dsfforza.formats import anark


def test_name_hash():
    # Anark's class and property hashes (h = (h + c) * 65599 from 0; keys keep 24 bits).
    assert anark.h32("Scene") == 0x068C6274 and anark.h32("Camera") == 0xF36612BB
    assert anark.h24("position.x") == 0x7C230D


def test_digi_data_is_what_the_engine_writes():
    # FUN_00891670: mph digits units first, (digit + 1) x 23/256, 0 for a leading blank.
    d = dd.digi_data(107, 3500, 3, 7000)
    assert abs(d[(2, 0)] - 8 * 23 / 256) < 1e-9 and abs(d[(2, 1)] - 1 * 23 / 256) < 1e-9
    assert abs(d[(2, 2)] - 2 * 23 / 256) < 1e-9 and d[(2, 3)] == 0.0
    assert abs(d[(1, 0)] - 0.25) < 1e-9 and abs(d[(6, 0)] - 3 / 7) < 1e-9
    assert dd.digi_data(5, 0, 0, 7000)[(2, 1)] == 0.0  # 5 mph: the tens cell is blank


def _patch():
    # A 0.2 x 0.1 m display in z = -0.7, UV0 0..1 across it, showing render-target
    # pixels 100..900 x 200..600 (transform scale 800/1280, 400/720).
    pts = [(0.0, 0.0, -0.7), (0.2, 0.0, -0.7), (0.2, 0.1, -0.7), (0.0, 0.1, -0.7)]
    uv = [(0.0, 1.0), (1.0, 1.0), (1.0, 0.0), (0.0, 0.0)]
    tris = [(0, 1, 2), (0, 2, 3)]
    t = (100 / 1280, 800 / 1280, 200 / 720, 400 / 720)
    return dd.DisplayPatch(
        "interior", 0, t, [[pts[i] for i in tr] for tr in tris], [[uv[i] for i in tr] for tr in tris], (0, 0, 1)
    )


def test_display_patch_maps_the_scene_onto_the_display():
    p = _patch()
    assert p.window() == (100.0, 900.0, 200.0, 600.0)
    centre = p.point(500, 400)
    assert all(abs(a - b) < 1e-9 for a, b in zip(centre, (0.1, 0.05, -0.7), strict=True))
    top_left = p.point(100, 200, lift=0.001)  # render-target y down, display v up
    assert all(abs(a - b) < 1e-9 for a, b in zip(top_left, (0.0, 0.1, -0.699), strict=True))
    outside = p.point(1000, 400)  # extrapolated from the nearest triangle
    assert abs(outside[0] - 0.225) < 1e-9


def test_preview_draws_the_cells_the_engine_picks():
    # One digit quad over cell 0 of a strip: the engine's offset shows cell digit + 1.
    W, H = 64, 512
    plan = dd.DigitalPlan(W, H, bytearray(W * H * 4))
    pitch = dd.DIGIT_PITCH * H
    for k in range(11):
        plan.fill(0, round(k * pitch) + 2, 8, round((k + 1) * pitch) - 2, (k * 20, 0, 0, 255))
    q = dd.Quad([(0, 0, 0)] * 4, [(0, 40 / H), (8 / W, 40 / H), (8 / W, 4 / H), (0, 4 / H)], dd.SPEED_DIGITS[0])
    q.rt = [(0, 10), (10, 10), (10, 0), (0, 0)]
    plan.quads.append(q)
    for mph in (3, 7):
        img = np.asarray(dd.preview(plan, mph, 0, 1, 7000, window=(0, 10, 0, 10), label=False))
        assert tuple(img[6, 6]) == ((mph + 1) * 20, 0, 0)


def _slanted_bar():
    # A slanted bar of radial-ish stripes 1 px wide, 3 px apart (the S2000's arc): each rpm
    # step lights a parallelogram of it.
    yy, xx = np.mgrid[0:60, 0:300]
    band = (yy > 0.15 * xx - 5) & (yy < 0.15 * xx + 20)
    stripe = (xx + yy // 4) % 3 == 0
    steps = np.where(band & stripe, (xx + yy) // 9, -1)
    ev_step = np.stack([steps, np.full_like(steps, -1)])
    ev_col = np.zeros((2, 60, 300, 3), np.float32)
    ev_col[0][steps >= 0] = (1.0, 0.4, 0.0)
    base = np.zeros((60, 300, 3), np.float32)
    return base, ev_step, ev_col


def test_bar_cells_are_coarse_and_smooth():
    # Stripes finer than a cell become one flat colour per cell, not stripes: the cells
    # inside the bar all light, at about the stripes' mean (a third of the lit colour).
    base, ev_step, ev_col = _slanted_bar()
    shown = np.ones(base.shape[:2], bool)
    _rest, colours, steps, drawn = dd.bar_cells(base, ev_step, ev_col, shown, 6)
    assert steps.shape == (2, 10, 50) and drawn.sum() > 60
    inner = drawn.copy()
    inner[1:-1, 1:-1] &= drawn[:-2, 1:-1] & drawn[2:, 1:-1] & drawn[1:-1, :-2] & drawn[1:-1, 2:]
    red = colours[0][..., 0][inner & np.pad(np.ones((8, 48), bool), 1)]
    assert len(red) and abs(red.mean() - 1 / 3) < 0.08 and red.std() < 0.05
    assert (steps[1] == -1).all() and (steps[0][drawn] >= 0).all()
    # off the displays nothing is drawn, and the blur takes no colour from there
    shown[:, 150:] = False
    _, colours2, _, drawn2 = dd.bar_cells(base, ev_step, ev_col, shown, 6)
    assert not drawn2[:, 25:].any()
    assert np.allclose(colours2[0][:, :20], colours[0][:, :20], atol=1e-4)


def test_lights_and_segments_are_one_piece_each():
    # Round shift lights on display triangles of their own (the Zonda Cinque's): every cell
    # round them is mostly off the display, so each light is one piece over its convex
    # hull (round, not a box), on the display and over nothing else.
    yy, xx = np.mgrid[0:40, 0:120]
    shown = np.zeros((40, 120), bool)
    steps = np.full((40, 120), -1)
    for k, cx in enumerate((20, 60, 100)):
        light = (xx - cx) ** 2 + (yy - 20) ** 2 <= 8**2
        shown |= light
        steps[light] = k
    ev_step = np.stack([steps, np.full_like(steps, -1)])
    ev_col = np.zeros((2, 40, 120, 3), np.float32)
    ev_col[0][steps >= 0] = (0.1, 0.9, 0.1)
    base = np.zeros((40, 120, 3), np.float32)
    _, _, _, drawn = dd.bar_cells(base, ev_step, ev_col, shown, 10)
    assert not drawn.any()
    detail = np.zeros((40, 120), bool)
    pieces, covered = dd.bar_segments(base, ev_step, ev_col, shown, detail, 10)
    assert [p[3][0] for p in pieces] == [0, 1, 2]
    for outline, rest, colours, _ in pieces:
        assert abs(dd._area(outline) - np.pi * 8**2) < 0.15 * np.pi * 8**2  # a disc, not its box
        assert colours[0][1] > 0.85 and rest.max() == 0
    assert not (covered & ~shown).sum() > 0.05 * covered.sum()
    # a segment the cells must stay off: no cell over a covered pixel
    _, _, _, drawn = dd.bar_cells(base, ev_step, ev_col, np.ones((40, 120), bool), 10, covered)
    assert not drawn.any()


def test_a_bar_along_a_display_edge_leaves_no_slivers():
    # A bar whose lower rows run off the display: what the cells leave becomes edge
    # pieces of a cell or more a side, or nothing.
    base, ev_step, ev_col = _slanted_bar()
    shown = np.ones(base.shape[:2], bool)
    shown[33:, :] = False
    ev_step[:, ~shown] = -1
    _, _, _, drawn = dd.bar_cells(base, ev_step, ev_col, shown, 6)
    in_cell = np.repeat(np.repeat(drawn, 6, 0), 6, 1)[:60, :300]
    for (x0, x1, y0, y1), *_ in dd.edge_pieces(base, ev_step, ev_col, shown, in_cell, 6):
        assert min(x1 - x0, y1 - y0) >= 6 and not in_cell[y0:y1, x0:x1].any()
        assert shown[y0:y1, x0:x1].mean() >= dd.EDGE_SHOWN


def test_bar_pieces_are_watertight_and_never_overlap():
    # Runs of cells along rows, with every neighbour's ends on their top and bottom edges:
    # no T-junction (a vertex rounded off its neighbour's edge opens a crack), no overlap.
    rng = np.random.default_rng(3)
    drawn = rng.random((12, 30)) > 0.2
    step = rng.integers(0, 4, (12, 30))
    column = rng.integers(0, 2, (12, 30))
    rows = dd.cell_runs(step, column, drawn)
    cover = np.zeros(drawn.shape, int)
    edges = {}  # (y, x0, x1) of every vertex-to-vertex segment along a row line -> count
    for i, row in enumerate(rows):
        for run in row:
            j0, j1, k, c = run
            assert (step[i, j0:j1] == k).all() and (column[i, j0:j1] == c).all()
            cover[i, j0:j1] += 1
            top, bottom = dd.run_outline(rows, i, run)
            tris = dd.zipper(top, bottom)
            pts = [(x, i) for x in top] + [(x, i + 1) for x in bottom]
            area = sum(
                abs(
                    (pts[b][0] - pts[a][0]) * (pts[c][1] - pts[a][1])
                    - (pts[c][0] - pts[a][0]) * (pts[b][1] - pts[a][1])
                )
                / 2
                for a, b, c in tris
            )
            assert abs(area - (j1 - j0)) < 1e-9  # the triangles tile the run, none degenerate
            assert all(len({pts[a], pts[b], pts[c]}) == 3 for a, b, c in tris)
            for y, chain in ((i, top), (i + 1, bottom)):
                for x0, x1 in itertools.pairwise(chain):
                    edges[(y, x0, x1)] = edges.get((y, x0, x1), 0) + 1
    assert (cover == drawn).all()
    # where two pieces meet along a row line, they cut it at the same vertices
    for (y, x0, x1), n in edges.items():
        if 0 < y < drawn.shape[0] and drawn[y - 1, x0] and drawn[y, x0]:
            assert n == 2, (y, x0, x1)


def test_pixel_size():
    assert abs(dd.pixel_size([_patch()]) - 0.2 / 800) < 1e-9


def test_live_quads_stay_on_the_display():
    # A round-ish display: its window's corners are off the triangles.
    p = _patch()
    p.uv0 = [p.uv0[0]]  # only the lower right triangle
    p.points = [p.points[0]]
    assert p.covers(800, 550) and not p.covers(150, 250)
    shown = dd.coverage([p], (100, 900, 200, 600), 800, 400)
    assert shown[350, 700] and not shown[50, 50]


def test_overlapping_digit_slots_meet_half_way():
    spans = dd._shown_spans([(20, 40, 0, 10), (0, 24, 0, 10), (38, 60, 0, 10)])
    assert spans == [(22.0, 39.0), (0, 22.0), (39.0, 60)]


def test_digit_cells_keep_to_the_display():
    # A display whose top edge slants down to the right: a cell over the edge loses the
    # rows above it (they would draw the black outside over the bezel).
    shown = np.zeros((40, 40), bool)
    for x in range(40):
        shown[10 + x // 4 :, x] = True
    (x0, x1), (y0, y1) = dd._shown_box(shown, (4.0, 12.0, 0.0, 30.0))
    assert (x0, x1) == (4.0, 12.0) and y0 == 12 and y1 == 30.0
    assert shown[int(y0) :, 4:12].all()


def test_texture_files():
    rgba = bytearray(range(256)) * 64  # 64 x 64 texels
    dxt = dd.dxt5_dds(64, 64, rgba)
    assert dxt[84:88] == b"DXT5" and len(dxt) == 128 + 64 * 64


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
