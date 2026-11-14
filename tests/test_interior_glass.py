"""Cockpit glass (glass.InteriorGlass): the cracks and shards of each converted window
are the host interior's own (#459).

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


def test_each_host_window_goes_to_one_window():
    """Two converted side windows on the left, one host door window: the nearer one
    takes its cracks; the other keeps its surface only (vanilla quarter lights)."""
    crack = (((0.8, 0.5, 0.2), (0.8, 0.5, 0.25), (0.8, 0.505, 0.25)), CRACK_UV, (0.5,) * 3)
    door = [(((0.8, 0.3, -0.3), (0.8, 0.3, 0.3), (0.8, 0.7, 0.3)), SMEAR_UV, (0.5,) * 3)]
    door += [(((0.8, 0.3, -0.3), (0.8, 0.7, 0.3), (0.8, 0.7, -0.3)), SMEAR_UV, (0.5,) * 3), crack]
    host = [glass.HostWindow((10, 13), door, [crack])]
    g = glass.InteriorGlass(host=host)
    for name, z0, z1 in (("glassLF", -0.3, 0.3), ("glassLR", -0.8, -0.45)):
        for pts in (
            ((0.8, 0.3, z0), (0.8, 0.3, z1), (0.8, 0.7, z1)),
            ((0.8, 0.3, z0), (0.8, 0.7, z1), (0.8, 0.7, z0)),
        ):
            g.add(name, pts, [(1.0, 0.0, 0.0)] * 3, [0.5] * 3, b"\x0a\x0d\x00\x01")
    regions = {}
    g.triangles()
    for group, info in g.report.items():
        regions[group.split("/")[0]] = info.get("host_window")
    assert regions == {"glasslf": [10, 13], "glasslr": None}
    assert host == []  # taken: a later InteriorGlass sharing the list doesn't get it


def test_no_host_window_on_that_side():
    """A right-hand host door doesn't go onto a left-hand window."""
    crack = (((-0.8, 0.5, 0.0), (-0.8, 0.5, 0.05), (-0.8, 0.505, 0.05)), CRACK_UV, (0.5,) * 3)
    host = [glass.HostWindow((8, 10), [crack], [crack])]
    g = glass.InteriorGlass(host=host)
    for pts in (
        ((0.8, 0.3, -0.3), (0.8, 0.3, 0.3), (0.8, 0.7, 0.3)),
        ((0.8, 0.3, -0.3), (0.8, 0.7, 0.3), (0.8, 0.7, -0.3)),
    ):
        g.add("glassLF", pts, [(1.0, 0.0, 0.0)] * 3, [0.5] * 3, b"\x0a\x0d\x00\x01")
    assert {r for _, r in g.triangles()} == {"surface"}


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
    assert [h.kind for h in host] == ["front", "rear"] and host[0] is front  # not taken


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
    assert host == [front]


def test_deeper_than_the_window_reaches():
    """A crack 0.3 m inside a 1.4 m wide host windscreen's left edge can't go 0.3 m inside a
    0.4 m wide window: it would cross the centre to the far side, so it's left out."""
    z = 1.0
    crack = (((0.38, 0.35, z), (0.40, 0.35, z), (0.40, 0.36, z)), CRACK_UV, (0.5,) * 3)
    host = [_host_window((0, 5), 1.4, 0.7, z, [crack])]
    g = _windscreen(0.4, 0.7, z, host)
    assert {r for _, r in g.triangles()} == {"surface"}


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
