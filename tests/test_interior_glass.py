"""Cockpit glass (glass.InteriorGlass): each converted window takes the cracks and shards
of the vanilla window of its kind that matches it best (#459).

    uv run python tests/test_interior_glass.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import itertools
import math

from dsfforza.convert import glass

SHARD_UV = ((0.08, 0.20), (0.10, 0.20), (0.09, 0.22))  # shard_bright block
CRACK_UV = ((0.02, 0.20), (0.03, 0.20), (0.03, 0.21))  # crack block
SMEAR_UV = ((0.02, 0.02), (0.05, 0.02), (0.05, 0.05))  # surface block


def _quad(x0, x1, y0, y1, z):
    """A windscreen-like pane facing +z: two triangles."""
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return [(a, b, c), (a, c, d)]


def _host_window(s9, width, height, z, overlay):
    tris = [(p, SMEAR_UV, (0.5,) * 3) for p in _quad(-width / 2, width / 2, 0.0, height, z)]
    return glass.HostWindow(s9, tris + overlay, overlay)


def _windscreen(width, height, z, host):
    g = glass.InteriorGlass(host=host)
    for pts in _quad(-width / 2, width / 2, 0.0, height, z):
        g.add("glassF", pts, [(0.0, 0.0, 1.0)] * 3, [0.5] * 3, b"\x00\x05\x00\x01")
    return g


def test_interior_blocks():
    """The strip's 64-texel blocks, as vanilla interiors use them."""
    assert glass.interior_block(SHARD_UV) == "shard_bright"
    assert glass.interior_block(CRACK_UV) == "crack"
    assert glass.interior_block(SMEAR_UV) == "surface"
    assert glass.interior_block(((0.05, 0.08), (0.11, 0.08), (0.08, 0.12))) == "shard_dull"
    assert glass.interior_block(((0.05, 0.02), (0.08, 0.2), (0.06, 0.2))) is None  # straddles


def test_shard_lands_in_the_same_corner_at_the_same_depth():
    """A shard 2 cm inside the host windscreen's top-left corner lands 2 cm inside the
    converted windscreen's top-left corner, with its size, UVs and the window's s9."""
    # host: 1.4 x 0.7 m; shard tip 2 cm from the left edge and 2 cm below the top
    z = 1.0
    shard = ((0.68, 0.68, z), (0.66, 0.68, z), (0.68, 0.66, z))  # +x is the car's left
    host = [_host_window((0, 5), 1.4, 0.7, z, [(shard, SHARD_UV, (0.7, 0.6, 0.5))])]
    g = _windscreen(1.0, 0.5, 1.1, host)
    tris = [(c, r) for c, r in g.triangles() if r != "surface"]
    assert len(tris) == 1
    corners, region = tris[0]
    assert region == "shard_bright"
    pos = [c[0] for c in corners]
    assert [c[1] for c in corners] == list(SHARD_UV)
    assert [c[3] for c in corners] == [0.7, 0.6, 0.5]  # the host's sky terms
    assert all(c[4] == b"\x00\x05\x00\x01" for c in corners)
    # same shape (2 cm legs), in the converted window's top-left corner, on its glass
    assert abs(math.dist(pos[0], pos[1]) - 0.02) < 1e-3
    assert abs(math.dist(pos[0], pos[2]) - 0.02) < 1e-3
    assert all(abs(p[2] - (1.1 - glass.LIFT)) < 1e-6 for p in pos)
    assert abs(pos[0][0] - 0.48) < 0.012 and abs(pos[0][1] - 0.48) < 0.012
    assert g.report["glassf/windscreen"]["host_window"] == [0, 5]


def _side_window(s9, x, z0, z1, overlay, car=None):
    """A vanilla door-like pane at x (facing along x), z0 to z1, 0.3 to 0.7 m high."""
    a, b, c, d = (x, 0.3, z0), (x, 0.3, z1), (x, 0.7, z1), (x, 0.7, z0)
    tris = [((a, b, c), SMEAR_UV, (0.5,) * 3), ((a, c, d), SMEAR_UV, (0.5,) * 3)]
    return glass.HostWindow(s9, tris + overlay, overlay, car=car)


def _add_side(g, section, x, z0, z1):
    for pts in (((x, 0.3, z0), (x, 0.3, z1), (x, 0.7, z1)), ((x, 0.3, z0), (x, 0.7, z1), (x, 0.7, z0))):
        g.add(section, pts, [(1.0 if x > 0 else -1.0, 0.0, 0.0)] * 3, [0.5] * 3, bytes((10, 13, 0, 1)))


def test_each_vanilla_window_goes_to_one_window():
    """Two converted front side windows on the left, one vanilla door: the one it fits
    best takes its cracks, the other keeps its surface only (no vanilla interior carries
    one set on two windows), and a later InteriorGlass sharing the Donors doesn't get it."""
    crack = (((0.8, 0.5, 0.5), (0.8, 0.5, 0.55), (0.8, 0.505, 0.55)), CRACK_UV, (0.5,) * 3)
    donors = glass.Donors(windows=[_side_window((10, 13), 0.8, 0.05, 0.65, [crack], car=7)])
    g = glass.InteriorGlass(host=donors)
    _add_side(g, "doorA", 0.8, 0.05, 0.65)  # the donor's own shape
    _add_side(g, "doorB", 0.8, 0.7, 0.9)
    g.triangles()
    took = {group.split("/")[0]: info.get("host_window") for group, info in g.report.items()}
    assert took == {"doora": [10, 13], "doorb": None}
    assert g.report["doora/side_front/l"]["donor_car"] == 7
    assert g.report["doora/side_front/l"]["donor_rms_mm"] == 0.0
    assert donors.taken == {(7, (10, 13))}
    later = glass.InteriorGlass(host=donors)
    _add_side(later, "doorC", 0.8, 0.05, 0.65)
    assert {r for _, r in later.triangles()} == {"surface"}


def test_the_window_takes_the_set_that_fits_it_best():
    """Two vanilla windscreens, 1.4 x 0.7 m (car 1) and 1.0 x 0.5 m (car 2): a 1.0 x 0.5 m
    converted windscreen takes car 2's set, which the move leaves where it is, whichever
    car the port's host is."""
    z = 1.0
    shard = (((0.48, 0.48, z), (0.46, 0.48, z), (0.48, 0.46, z)), SHARD_UV, (0.5,) * 3)
    big = _host_window((0, 5), 1.4, 0.7, z, [shard])
    small = _host_window((0, 3), 1.0, 0.5, z, [shard])
    big.car, small.car = 1, 2
    g = _windscreen(1.0, 0.5, 1.1, [big, small])
    tris = [corners for corners, r in g.triangles() if r != "surface"]
    assert len(tris) == 1
    info = g.report["glassf/windscreen"]
    assert (info["donor_car"], info["host_window"], info["donor_rms_mm"]) == (2, [0, 3], 0.0)


def test_mismatch_is_how_far_the_set_moves():
    """The RMS move of a set's corners: 0 onto a window of its own shape; onto a window
    2 cm wider on each side, a piece at the left edge moves 2 cm with the rim."""
    z = 1.0
    shard = (((0.70, 0.35, z), (0.69, 0.35, z), (0.70, 0.34, z)), SHARD_UV, (0.5,) * 3)
    d = glass.donor_set(_host_window((0, 5), 1.4, 0.7, z, [shard]))
    same = glass._Rim(_windscreen(1.4, 0.7, z, []).panes()["glassf/windscreen"])
    wider = glass._Rim(_windscreen(1.44, 0.7, z, []).panes()["glassf/windscreen"])
    assert glass.mismatch(d, same) == (0.0, 0.0)
    rms, dropped = glass.mismatch(d, wider)
    assert abs(rms - 0.02) < 0.002 and dropped == 0.0


def test_side_glass_behind_the_eye_takes_none():
    """Side glass wholly behind the driver's eye keeps its surface only, even beside a
    vanilla window of its own shape (vanilla: 2 of 74 such windows have a set); the door
    beside the eye takes one whatever Forza calls it (glassLM on cars with a vent)."""
    crack = (((0.8, 0.5, -0.6), (0.8, 0.5, -0.55), (0.8, 0.505, -0.55)), CRACK_UV, (0.5,) * 3)
    crack2 = (((0.8, 0.5, 0.0), (0.8, 0.5, 0.05), (0.8, 0.505, 0.05)), CRACK_UV, (0.5,) * 3)
    donors = glass.Donors(
        windows=[
            _side_window((10, 13), 0.8, -0.8, -0.45, [crack], car=1),
            _side_window((10, 13), 0.8, -0.3, 0.3, [crack2], car=2),
        ],
        eye_z=-0.1,
    )
    g = glass.InteriorGlass(host=donors)
    _add_side(g, "glassLR", 0.8, -0.8, -0.45)  # behind the eye
    _add_side(g, "glassLM", 0.8, -0.3, 0.3)  # the door, spanning the eye
    g.triangles()
    took = {group.split("/")[0]: info.get("donor_car") for group, info in g.report.items()}
    assert took == {"glasslr": None, "glasslm": 2}


def test_side_glass_behind_the_eye_is_on_its_exterior_slot():
    """Quarter glass behind the driver's eye draws on its window's exterior slot (side
    rear for glassLR), mapped as the exterior glass and filling the slot, on its own
    panel record: its crack bands then show from the cockpit, as on vanilla interiors
    (#627); the door beside the eye keeps the smear rows."""
    g = glass.InteriorGlass(host=glass.Donors(windows=[], eye_z=-0.1))
    _add_side(g, "glassLR", 0.8, -0.8, -0.45)
    _add_side(g, "glassLM", 0.8, -0.3, 0.3)
    tris = g.triangles()
    quarter = [c for c, r in tris if r == "slot:side_rear"]
    door = [c for c, r in tris if r == "surface"]
    assert len(quarter) == 2 and len(door) == 2
    u0, u1, v0, v1 = glass.SLOTS["side_rear"]
    uvs = [c[1] for t in quarter for c in t]
    assert all(u0 - 1e-9 <= u <= u1 + 1e-9 and v0 - 1e-9 <= v <= v1 + 1e-9 for u, v in uvs)
    assert {round(u, 6) for u, _ in uvs} == {u0, u1} and {round(v, 6) for _, v in uvs} == {v0, v1}
    assert all(c[4] == bytes((10, 13, 0, 1)) for t in quarter for c in t)
    assert all(glass._interior_region([c[1] for c in t]) == "slot:side_rear" for t in quarter)
    assert all(glass.interior_block([c[1] for c in t]) == "surface" for t in door)
    assert g.report["glasslr/side_rear"]["surface"] == "slot:side_rear"


def test_fresh_donors_forget_what_was_taken():
    """Each conversion attempt starts with every vanilla window free."""
    crack = (((0.8, 0.5, 0.5), (0.8, 0.5, 0.55), (0.8, 0.505, 0.55)), CRACK_UV, (0.5,) * 3)
    donors = glass.Donors(windows=[_side_window((10, 13), 0.8, 0.05, 0.65, [crack], car=7)])
    donors.taken.add((7, (10, 13)))
    again = donors.fresh(eye_z=0.0)
    assert not again.taken and again.eye_z == 0.0 and again.sets == donors.sets
    assert again.window(again.sets[0]) is donors.window(donors.sets[0])


def test_a_record_with_panes_on_both_sides_is_no_donor():
    """One vanilla record holding the left and right quarter lights would put both panes'
    pieces on one window."""
    crack = (((0.8, 0.5, -0.6), (0.8, 0.5, -0.55), (0.8, 0.505, -0.55)), CRACK_UV, (0.5,) * 3)
    left = _side_window((0, 0), 0.8, -0.8, -0.45, [crack])
    right = _side_window((0, 0), -0.8, -0.8, -0.45, [])
    both = glass.HostWindow((0, 0), left.tris + right.tris, [crack])
    assert both.side is None and glass.donor_set(both) is None
    assert left.side == 1 and right.side == -1


def test_rear_window_does_not_take_the_windscreen():
    """The host's rear glass has no cracks or shards: the converted rear window keeps its
    surface only rather than taking the host windscreen's (CodeRabbit, #608)."""
    shard = (((0.6, 0.6, 1.0), (0.58, 0.6, 1.0), (0.6, 0.58, 1.0)), SHARD_UV, (0.5,) * 3)
    front = _host_window((0, 5), 1.4, 0.7, 1.0, [shard])
    rear = glass.HostWindow((0, 22), [(p, SMEAR_UV, (0.5,) * 3) for p in _quad(-0.6, 0.6, 0.0, 0.4, -1.2)], [])
    host = [front, rear]
    g = glass.InteriorGlass(host=host)
    for pts in _quad(-0.5, 0.5, 0.0, 0.4, -1.1):
        g.add("glassR", pts, [(0.0, 0.0, -1.0)] * 3, [0.5] * 3, bytes((0, 22, 0, 1)))
    assert {r for _, r in g.triangles()} == {"surface"}
    assert [h.kind for h in host] == ["front", "rear"] and not g.donors.taken


def test_rear_window_takes_the_host_rear_layers():
    """The host's rear glass has shards: the converted rear window takes all of them,
    whether or not the driver's eye sees them (the host draws them all; #608)."""
    z = -1.2
    shards = [(((x, 0.38, z), (x - 0.02, 0.38, z), (x, 0.36, z)), SHARD_UV, (0.5,) * 3) for x in (-0.5, 0.0, 0.5)]
    front = _host_window((0, 5), 1.4, 0.7, 1.0, [])
    rear = _host_window((0, 22), 1.2, 0.4, z, shards)
    host = [front, rear]
    g = glass.InteriorGlass(host=host)
    for pts in _quad(-0.5, 0.5, 0.0, 0.4, -1.1):
        g.add("glassR", pts, [(0.0, 0.0, -1.0)] * 3, [0.5] * 3, bytes((0, 22, 0, 1)))
    layers = [r for _, r in g.triangles() if r != "surface"]
    assert layers == ["shard_bright"] * 3
    assert g.report["glassr/rear"]["host_window"] == [0, 22]
    assert g.donors.taken == {(None, (0, 22))}


def test_deeper_than_the_window_reaches():
    """A crack 0.3 m inside a 1.4 m wide host windscreen's left edge can't go 0.3 m inside a
    0.4 m wide window: it would cross the centre to the far side, so it's left out."""
    z = 1.0
    crack = (((0.38, 0.35, z), (0.40, 0.35, z), (0.40, 0.36, z)), CRACK_UV, (0.5,) * 3)
    host = [_host_window((0, 5), 1.4, 0.7, z, [crack])]
    g = _windscreen(0.4, 0.7, z, host)
    assert {r for _, r in g.triangles()} == {"surface"}


def test_a_shard_reaching_over_the_frame_is_kept():
    """Vanilla shards are rooted at the glass's edge and reach past it, over the frame
    (#626): a shard 4 cm past the host windscreen's top edge stays on a converted
    windscreen of the same shape, where the old test (a corner at most one 1 cm cell
    outside) dropped it."""
    z = 1.0
    shard = (((0.0, 0.69, z), (0.02, 0.69, z), (0.01, 0.74, z)), SHARD_UV, (0.5,) * 3)
    host = [_host_window((0, 5), 1.4, 0.7, z, [shard])]
    assert [round(x, 3) for x in host[0].overhang[0]] == [0.0, 0.0, 0.04, 0.007]
    g = _windscreen(1.4, 0.7, z, host)
    assert [r for _, r in g.triangles() if r != "surface"] == ["shard_bright"]


def test_a_shard_no_further_past_the_glass_than_on_its_donor():
    """The window's extent for a triangle is the glass plus how far that triangle reached
    past its own window's glass, to one grid cell: a corner landing 3 cm past the glass
    is dropped when it was on the donor's glass, kept when it was 3 cm past it there."""
    pane = _windscreen(1.0, 0.5, 1.0, []).panes()["glassf/windscreen"]
    q = [_s(pane, x, y) for x, y in ((0.0, 0.48), (0.02, 0.48), (0.01, 0.53))]
    assert abs(pane.past(q[2]) - 0.03) < 1e-9 and pane.past(q[0]) == 0.0
    tri = (None, SHARD_UV, (0.5,) * 3)
    args = (pane, tri, q, q[0], q[0], 0.0, b"\0\0\0\0", [(0.0, 0.0, 1.0)] * 3, [0.0] * 3)
    assert glass.InteriorGlass._place(*args, (0.0, 0.0, 0.0, 0.0)) == []
    assert len(glass.InteriorGlass._place(*args, (0.0, 0.0, 0.03, 0.0))) == 1


def _s(pane, x, y):
    """The plane point of the car-space point (x, y) on a windscreen facing +z."""
    p = (x, y, 1.0)
    return (glass._dot(p, pane.a), glass._dot(p, pane.b))


def test_the_window_glass_is_closed_by_the_exterior():
    """A vanilla windscreen's interior smear is a frame with an open centre; the exterior
    glass on its panel record is the whole pane (#626): a crack in the centre is on the
    window's glass, not 0.2 m past it."""
    z = 1.0
    top = [(p, SMEAR_UV, (0.5,) * 3) for p in _quad(-0.7, 0.7, 0.55, 0.7, z)]
    crack = (((0.0, 0.3, z), (0.05, 0.3, z), (0.05, 0.303, z)), CRACK_UV, (0.5,) * 3)
    open_centre = glass.HostWindow((0, 5), [*top, crack], [crack])
    closed = glass.HostWindow((0, 5), [*top, crack], [crack], exterior=_quad(-0.7, 0.7, 0.0, 0.7, z + 0.005))
    assert min(open_centre.overhang[0]) > 0.2
    assert closed.overhang[0] == (0.0, 0.0, 0.0, 0.0)


def test_a_crack_line_moves_as_one_piece():
    """A crack line of slivers sharing vertices, rooted at the host's left edge and running
    in towards the centre at an angle, keeps its shape on a smaller window: the slivers
    stay joined, as vanilla windscreens that share a crack pattern keep them (#459)."""
    z = 1.0
    line = [(0.70 - 0.03 * i, 0.30 + 0.012 * i) for i in range(9)]  # 24 cm, 22 degrees off the axis
    crack = []
    for (x0, y0), (x1, y1) in itertools.pairwise(line):
        crack.append((((x0, y0, z), (x1, y1, z), (x1, y1 + 0.003, z)), CRACK_UV, (0.5,) * 3))
    host = [_host_window((0, 5), 1.4, 0.7, z, crack)]
    g = _windscreen(1.0, 0.5, z, host)
    tris = [[c[0] for c in corners] for corners, r in g.triangles() if r == "crack"]
    assert len(tris) == len(crack)
    for a, b in itertools.pairwise(tris):
        assert math.dist(a[1], b[0]) < 1e-6  # still joined
    ends = (tris[0][0], tris[-1][1])
    assert abs(math.dist(*ends) - math.dist(line[0], line[-1])) < 1e-6  # same length


def test_shards_keep_the_host_facing():
    """A host shard facing into the cabin (-z on a windscreen facing +z) and one facing
    out keep their own facing on the converted window, as normals: vanilla stores each
    crack and shard vertex's own triangle facing, not the glass's outward normal."""
    z = 1.0
    inward = ((0.68, 0.68, z), (0.68, 0.66, z), (0.66, 0.68, z))  # winding faces -z
    outward = ((-0.68, 0.68, z), (-0.68, 0.66, z), (-0.66, 0.68, z))  # faces +z
    overlay = [(inward, SHARD_UV, (0.5,) * 3), (outward, SHARD_UV, (0.5,) * 3, ((0.0, 0.0, 1.0),) * 3)]
    host = [_host_window((0, 5), 1.4, 0.7, z, overlay)]
    g = _windscreen(1.0, 0.5, 1.1, host)
    tris = [corners for corners, r in g.triangles() if r != "surface"]
    assert len(tris) == 2
    by_side = {corners[0][0][0] > 0: corners for corners in tris}
    for left, sign in ((True, -1.0), (False, 1.0)):
        for corner in by_side[left]:
            assert abs(corner[2][2] - sign) < 1e-6, (left, corner[2])
        pts = [c[0] for c in by_side[left]]
        facing = glass._cross(glass._sub(pts[1], pts[0]), glass._sub(pts[2], pts[0]))
        assert facing[2] * sign > 0  # the winding came along too: normal and facing agree


def test_a_crack_fin_keeps_its_height():
    """Vanilla crack lines are fins standing across the glass: a crack quad rooted on
    the host windscreen (z 1.0) and reaching 4 mm out of it (+z, the window's outward
    side) keeps its 4 mm on the converted window instead of being flattened onto the
    glass, where it would have no area."""
    z, h = 1.0, 0.004
    root = ((0.60, 0.60, z), (0.62, 0.60, z))
    top = ((0.60, 0.60, z + h), (0.62, 0.60, z + h))
    fin = [(root[0], root[1], top[1]), (root[0], top[1], top[0])]
    host = [_host_window((0, 5), 1.4, 0.7, z, [(t, CRACK_UV, (0.5,) * 3) for t in fin])]
    g = _windscreen(1.0, 0.5, 1.1, host)
    tris = [[c[0] for c in corners] for corners, r in g.triangles() if r == "crack"]
    assert len(tris) == 2
    zs = sorted({round(p[2], 6) for t in tris for p in t})
    assert zs == [round(1.1 - glass.LIFT, 6), round(1.1 - glass.LIFT + h, 6)]
    area = sum(
        math.sqrt(glass._dot(c, c)) / 2
        for c in (glass._cross(glass._sub(t[1], t[0]), glass._sub(t[2], t[0])) for t in tris)
    )
    assert abs(area - 0.02 * h) < 1e-6


def test_a_wide_triangular_fin_keeps_its_apex_height():
    """A triangular fin 4 cm wide, its apex 4 mm out of the host windscreen over the
    middle of its root: no other host vertex lies within 2 cm of the apex (the host's
    glass there is only its far corners), and the apex still keeps its 4 mm on the
    converted window while both root corners land on the glass."""
    z, h = 1.0, 0.004
    fin = ((0.60, 0.60, z), (0.64, 0.60, z), (0.62, 0.60, z + h))
    host = [_host_window((0, 5), 1.4, 0.7, z, [(fin, CRACK_UV, (0.5,) * 3)])]
    g = _windscreen(1.0, 0.5, 1.1, host)
    tris = [[c[0] for c in corners] for corners, r in g.triangles() if r == "crack"]
    assert len(tris) == 1
    zs = sorted(round(p[2], 6) for p in tris[0])
    assert zs == [round(1.1 - glass.LIFT, 6)] * 2 + [round(1.1 - glass.LIFT + h, 6)]


def test_fin_heights_are_above_the_fin_root():
    """Each corner's height is how far it stands outward of the nearest corner it
    shares a triangle with: a slab wall's top over its root, 0 at the root, and the
    window's own tilt along a long root line does not raise either root."""
    n = (0.0, 0.0, 1.0)
    # wall 3 mm high on glass tilted 1 in 5 along x (the root line climbs 6 mm over 3 cm)
    r0, r1 = (0.0, 0.0, 0.0), (0.03, 0.0, 0.006)
    t0, t1 = (0.0, 0.0, 0.003), (0.03, 0.0, 0.009)
    heights = glass._fin_heights([(r0, r1, t1), (r0, t1, t0)], n)
    assert [[round(x, 6) for x in hs] for hs in heights] == [[0.0, 0.0, 0.003], [0.0, 0.003, 0.003]]


def test_pieces():
    """Triangles sharing a corner (each coordinate within one 1/4096 m position step)
    are one piece; corners two steps apart, or a corner over another along the
    normal (a layer above it), are not."""
    s = glass.PIECE_STEP
    a = ((0, 0, 0), (1, 0, 0), (0, 1, 0))
    b = ((1 + s, 0, -s), (2, 0, 0), (1, 1, 0))  # one step on two axes: a's corner
    c = ((2 + 2 * s, 0, 0), (3, 0, 0), (2, 1, 0))  # two steps from b's
    d = ((0, 1, 12 * s), (0, 2, 0), (-1, 2, 0))  # 3 mm over a's corner
    assert sorted(sorted(p) for p in glass._pieces([a, b, c, d])) == [[0, 1], [2], [3]]


def test_cockpit_glass_takes_the_lexan_binding():
    """The MC12's cockpit carbin names its windscreen glassF where the main carbin says
    lexanF: the cockpit pane takes lexanF's panel, not the chassis (#459)."""
    from types import SimpleNamespace

    from dsfforza.convert.forza2dsf.vertices import interior_glass_skin

    sub = SimpleNamespace(lod=1)
    lexan = SimpleNamespace(name="lexanF", subsections=[sub])
    glass_own = SimpleNamespace(name="glassR", subsections=[SimpleNamespace(lod=1)])
    lexan_r = SimpleNamespace(name="lexanR", subsections=[SimpleNamespace(lod=1)])
    car = SimpleNamespace(body=[lexan, glass_own, lexan_r])
    pairs = {id(sub): (0, 4), id(glass_own.subsections[0]): (0, 20), id(lexan_r.subsections[0]): (0, 21)}
    dmg = SimpleNamespace(assignment=lambda *_: {(k, 0): v for k, v in pairs.items()}, bound=lambda pair: pair)
    skin = interior_glass_skin(dmg, car, SimpleNamespace(lods={"full": 1}))
    assert skin["glassF"] == skin["lexanF"] == bytes((0, 4, 0, 1))
    assert skin["glassR"] == bytes((0, 20, 0, 1))  # a glass section of its own keeps its own


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
