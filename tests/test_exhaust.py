"""Checks for exhaust (exhaust markers on the Forza tailpipes); no game files needed.

uv run python tests/test_exhaust.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import math
import struct

from dsfforza.convert import exhaust
from dsfforza.convert.forza2dsf import Placement
from dsfforza.formats import carbin, upvh

MIRROR = struct.pack("<4f", 1.0, 2.0, 3.0, 4.0)  # a table after the markers, to check it moves along


def _record(type_: int, role: int, subtree: int, marker: int = -1) -> bytes:
    rec = bytearray(upvh.RECORD_SIZE)
    rec[:8] = upvh.NONE_UID
    struct.pack_into("<HH", rec, 0x08, type_, role)
    struct.pack_into("<H", rec, 0x14, subtree)
    struct.pack_into("<hhh", rec, 0x1A, marker, -1, -1)
    return bytes(rec)


def _host(exhausts: list[tuple[int, tuple]]) -> bytes:
    """A UPVH like a stock car's rear: root, then per (part role, marker
    positions) an exhaust part with its role-0x4D markers, then a steering
    marker (role 47), one mirror entry and an empty PDL."""
    records, table = [], []
    for role, positions in exhausts:
        records.append(_record(1, role, 1 + len(positions)))
        for pos in positions:
            records.append(_record(3, exhaust.ROLE, 1, len(table)))
            table.append(struct.pack("<3f4x4f", *pos, 0.0, 0.0, 0.0, 1.0))
    records.append(_record(3, 47, 1, len(table)))
    table.append(struct.pack("<3f4x4f", 0.4, 0.2, 0.4, 0.0, 0.0, 0.0, 1.0))
    records.insert(0, _record(4, 0, len(records) + 1))
    head = bytearray(upvh.HEADER_SIZE)
    head[:4] = b"AWHF"
    struct.pack_into("<6I", head, 4, 0, 0, 26, len(records), 1, 0)
    struct.pack_into("<H", head, 0x28, len(table))
    head[0x2D] = 1  # mirrors
    return bytes(head) + b"".join(records) + b"".join(table) + MIRROR


TWIN_HOST = [(136, [(-0.49, -0.04, -1.96)]), (15, [(0.49, -0.04, -1.96)])]


def _steering(data: bytes) -> tuple:
    h = upvh.parse(data)
    rec = next(r for r in h.records if r.type == 3 and r.role == 47)
    return struct.unpack_from("<3f", h.table("markers")[rec.marker])


def test_insert_and_remove_marker_records():
    data = _host(TWIN_HOST)
    out = exhaust.insert_marker(data, 2)  # a second marker under the -x exhaust part (record 1)
    h = upvh.parse(out)
    assert len(h.records) == 7 and h.counts["markers"] == 4
    assert h.records[0].subtree == 7 and h.records[1].subtree == 3 and h.records[4].subtree == 2
    assert exhaust.parents(h)[3] == 1 and h.records[3].marker == 3
    assert [m.position for m in exhaust.host_markers(out)][1] == exhaust.host_markers(data)[0].position
    assert _steering(out) == _steering(data) and out.endswith(MIRROR)
    assert exhaust.remove_marker(out, 3) == data
    # Dropping the first marker renumbers the ones after it.
    dropped = exhaust.remove_marker(data, 2)
    h = upvh.parse(dropped)
    assert h.counts["markers"] == 2 and len(h.records) == 5 and h.records[0].subtree == 5
    assert _steering(dropped) == _steering(data) and dropped.endswith(MIRROR)


def test_set_count_keeps_sides():
    quad = _host([(136, [(-0.6, -0.4, -2.2), (-0.5, -0.4, -2.2)]), (15, [(0.5, -0.4, -2.2), (0.6, -0.4, -2.2)])])
    two = exhaust.set_count(quad, 2)
    assert sorted(round(m.position[0], 4) for m in exhaust.host_markers(two)) == [-0.6, 0.6]  # the outer pair
    one = exhaust.set_count(_host([(136, [(-0.49, 0.0, -2.0)])]), 3)
    assert len(exhaust.host_markers(one)) == 3
    assert exhaust.set_count(_host([]), 2) == _host([])  # a host without exhausts gets none


def test_quaternion_turns_minus_z_onto_the_flow():
    assert exhaust.quaternion(exhaust.BACK) == (0.0, 0.0, 0.0, 1.0)
    # Stock Classic 06's right-hand side exit: (0, -0.71, 0, 0.71).
    q = exhaust.quaternion((1.0, 0.0, 0.0))
    assert max(abs(a - b) for a, b in zip(q, (0.0, -math.sqrt(0.5), 0.0, math.sqrt(0.5)))) < 1e-9


# --- Forza geometry (Forza space: front at -z; Placement(0, 0) turns it to DSF) -----------


def _section(name: str, tris: list[tuple]) -> carbin.Section:
    verts, idx = [], []
    for tri in tris:
        for p in tri:
            idx.append(len(verts))
            verts.append(carbin.Vertex(p, (0.0, 0.0), (0.0, 0.0), (0.0, 1.0, 0.0)))
    sub = carbin.SubSection("chrome", 1, 4, (0, 1, 0, 1, 0, 1, 0, 1), idx)
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def _tube(centre, radius, z0, z1, axis=2, n=12) -> list[tuple]:
    """An open tube along Forza z (axis 2) or x (axis 0) from z0 to z1."""
    tris = []
    for k in range(n):
        ring = []
        for a in (2 * math.pi * k / n, 2 * math.pi * (k + 1) / n):
            u, v = radius * math.cos(a), radius * math.sin(a)
            for t in (z0, z1):
                if axis == 2:
                    ring.append((centre[0] + u, centre[1] + v, t))
                else:
                    ring.append((t, centre[1] + v, centre[2] + u))
        a0, a1, b0, b1 = ring
        tris += [(a0, a1, b0), (b0, a1, b1)]
    return tris


def _body(half_width=0.9, length=2.1, height=1.3, step=0.1) -> carbin.Section:
    """A box of small triangles: the body the gaps are measured to (Forza rear at +z)."""
    tris = []
    count = int(2 * length / step)
    for i in range(count + 1):
        z = -length + i * step
        for j in range(int(height / step) + 1):
            y = j * step
            for x in (-half_width, half_width):
                tris.append(((x, y, z), (x, y + 0.01, z), (x, y, z + 0.01)))
    for i in range(int(2 * half_width / step) + 1):
        x = -half_width + i * step
        for j in range(int(height / step) + 1):
            y = j * step
            for z in (-length, length):
                tris.append(((x, y, z), (x + 0.01, y, z), (x, y + 0.01, z)))
    return _section("body", tris)


def test_dual_tips_at_the_rear():
    body = [
        _body(),
        _section("exhaustLa", _tube((0.45, 0.2), 0.04, 1.6, 2.15)),
        _section("exhaustRa", _tube((-0.45, 0.2), 0.04, 1.6, 2.15) + _tube((-0.45, 0.2), 0.03, 1.8, 2.12)),
    ]
    tips = exhaust.find_tips(body, Placement(0.0, 0.0))
    assert len(tips) == 2, tips
    for tip, x in zip(tips, (-0.45, 0.45)):  # DSF x is Forza -x
        assert abs(tip.centre[0] - x) < 1e-3 and abs(tip.centre[1] - 0.2) < 1e-3
        assert abs(tip.centre[2] + 2.15) < 1e-6 and tip.direction == exhaust.BACK


def test_side_exit_points_out():
    # A GT1-style pipe ending at the body's side behind the front wheel (Forza -z is the front).
    body = [_body(), _section("exhausta", _tube((0.0, 0.25, -0.7), 0.05, 0.6, 0.92, axis=0))]
    (tip,) = exhaust.find_tips(body, Placement(0.0, 0.0))
    assert tip.direction == (-1.0, 0.0, 0.0) and abs(tip.centre[0] + 0.92) < 1e-6
    assert abs(tip.centre[2] - 0.7) < 1e-3  # DSF z = -Forza z


def test_twin_opening_takes_both_host_markers():
    # One piece with two openings side by side (the Focus ST's trapezoid): two markers at its halves.
    twin = _tube((0.07, 0.15), 0.035, 1.9, 2.1) + _tube((-0.07, 0.15), 0.035, 1.9, 2.1)
    bridge = [((-0.035, 0.15, 2.1), (0.035, 0.15, 2.1), (0.0, 0.15, 2.09))]  # joins the two rims
    bridge += [((0.035, 0.15, 2.1), (0.035, 0.15, 2.099), (0.0, 0.15, 2.09))]
    bridge += [((-0.035, 0.15, 2.1), (-0.035, 0.15, 2.099), (0.0, 0.15, 2.09))]
    tips = exhaust.find_tips([_body(), _section("exhausta", twin + bridge)], Placement(0.0, 0.0))
    assert len(tips) == 1 and abs(tips[0].size[0] - 0.21) < 1e-3, tips
    out, report = exhaust.patch_upvh(_host(TWIN_HOST), tips)
    ms = sorted(exhaust.host_markers(out), key=lambda m: m.position[0])
    assert [round(m.position[0], 4) for m in ms] == [-0.0525, 0.0525] and "count" not in report
    assert all(m.quaternion == (0.0, 0.0, 0.0, 1.0) for m in ms)


def test_marker_count_follows_the_tips():
    def tip(x):
        return exhaust.Tip((x, -0.3, -2.2), exhaust.BACK, (0.08, 0.08))

    quad = _host([(136, [(-0.6, -0.4, -2.2), (-0.5, -0.4, -2.2)]), (15, [(0.5, -0.4, -2.2), (0.6, -0.4, -2.2)])])
    out, report = exhaust.patch_upvh(quad, [tip(0.0)])
    assert report["count"] == [4, 1] and [m.position for m in exhaust.host_markers(out)] == [
        tuple(struct.unpack("<3f", struct.pack("<3f", 0.0, -0.3, -2.2)))
    ]
    out, report = exhaust.patch_upvh(_host(TWIN_HOST), [tip(x) for x in (-0.6, -0.5, 0.5, 0.6)])
    assert report["count"] == [2, 4]
    assert sorted(round(m.position[0], 3) for m in exhaust.host_markers(out)) == [-0.6, -0.5, 0.5, 0.6]
    assert _steering(out) == _steering(quad) and out.endswith(MIRROR)
    # Six tips on four markers: the nearest neighbours share one.
    out, report = exhaust.patch_upvh(quad, [tip(x) for x in (-0.7, -0.6, -0.2, 0.2, 0.6, 0.7)])
    assert len(exhaust.host_markers(out)) == 4


def test_no_tip_keeps_or_maps_the_host_markers():
    from dsfforza.convert.damage import Box

    data = _host(TWIN_HOST)
    out, report = exhaust.patch_upvh(data, [])
    assert out == data and report["kept"] == "no Forza tip"
    boxes = (Box((-0.9, -0.5, -2.0), (0.9, 1.0, 2.0)), Box((-1.0, -0.5, -2.2), (1.0, 1.0, 2.2)))
    out, report = exhaust.patch_upvh(data, [], boxes)
    xs = sorted(round(m.position[0], 4) for m in exhaust.host_markers(out))
    assert xs == [round(-0.49 / 0.9, 4), round(0.49 / 0.9, 4)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
