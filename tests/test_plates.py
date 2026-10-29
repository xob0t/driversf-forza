"""Licence plate placement (src/dsfforza/convert/plates.py): the fallback plate
with no face to sit on (step 3) keeps off badges, lamps and section boundaries
(#159: the Mazda 2's front plate over its emblem and bonnet edge)."""

from __future__ import annotations

import itertools

from dsfforza.convert import plates as P

SIZE = (0.26, 0.12)


def quad(x0, x1, y0, y1, z, cls=1, name="bumperFa", feat=0):
    """Two triangles of a vertical rectangle facing +z (the front), DSF coordinates."""
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return [([a, b, c], cls, name, feat), ([a, c, d], cls, name, feat)]


def front_map(tris):
    return P.depth_map(tris, "front", (-0.6, 0.6), (-0.1, 1.0))


def test_plain_bumper_keeps_the_host_height():
    dm = front_map(quad(-0.5, 0.5, 0.0, 0.8, 2.0))
    y, _, note = P.fallback_height(dm, 0.40, SIZE)
    assert abs(y - 0.40) < 0.015
    assert note == "no flat face of plate size"


def test_plate_keeps_off_a_badge():
    # An emblem 1 cm proud of the bumper, at the host's plate height.
    tris = quad(-0.08, 0.08, 0.35, 0.45, 2.01, cls=4, feat=P.FEAT_BADGE) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    dm = front_map(tris)
    y, _, _ = P.fallback_height(dm, 0.40, SIZE)
    # Clear of the badge: the plate's edge beyond the emblem's.
    assert y + SIZE[1] / 2 <= 0.355 or y - SIZE[1] / 2 >= 0.445, y


def test_plate_keeps_off_a_lamp():
    tris = quad(-0.3, 0.3, 0.30, 0.42, 2.005, cls=4, feat=P.FEAT_LAMP) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    y, _, _ = P.fallback_height(front_map(tris), 0.36, SIZE)
    assert y + SIZE[1] / 2 <= 0.305 or y - SIZE[1] / 2 >= 0.415, y


def test_plate_stays_below_the_bonnet_shut_line():
    # Bumper face up to y 0.45, the bonnet above it a little further back.
    tris = quad(-0.5, 0.5, 0.0, 0.45, 2.0) + quad(-0.5, 0.5, 0.45, 0.8, 1.995, name="hooda")
    y, _, _ = P.fallback_height(front_map(tris), 0.44, SIZE)
    assert y + SIZE[1] / 2 <= 0.455, y


def test_plate_may_cross_a_grille():
    # A grille mesh across the whole bumper centre: a plate goes on it (bracket),
    # at the host's height.
    tris = quad(-0.3, 0.3, 0.1, 0.6, 2.0, cls=2, feat=P.FEAT_GRILLE) + quad(-0.5, 0.5, 0.0, 0.8, 2.01)
    y, _, _ = P.fallback_height(front_map(tris), 0.35, SIZE)
    assert abs(y - 0.35) < 0.015


def test_badge_band_is_grille_artwork():
    # A badge-technique band across the whole nose (the BMW M1's kidneys and grille
    # strip) is no emblem: the plate stays at the host's height on it.
    tris = quad(-0.45, 0.45, 0.30, 0.50, 2.002, cls=4, feat=P.FEAT_BADGE) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    y, _, _ = P.fallback_height(front_map(tris), 0.40, SIZE)
    assert abs(y - 0.40) < 0.015


def test_run_widths():
    import numpy as np

    mask = np.array([[0, 1, 1, 0, 1], [1, 1, 1, 1, 1]], bool)
    assert P.run_widths(mask).tolist() == [[0, 2, 2, 0, 1], [5, 5, 5, 5, 5]]


def test_feature_of_techniques():
    assert P.feature("badge") == P.FEAT_BADGE
    assert P.feature("emblem") == P.FEAT_BADGE
    assert P.feature("grille1_alpha") == P.FEAT_GRILLE
    assert P.feature("head_light") == P.FEAT_LAMP
    assert P.feature("lights_gls_tail_light") == P.FEAT_LAMP
    assert P.feature("body") == 0
    assert P.feature("plastic2") == 0


def slope(x0, x1, y0, y1, z0, z1, cls=1, name="bumperFa"):
    """Two triangles of a rectangle from (y0, z0) to (y1, z1) facing +z (the front)."""
    a, b, c, d = (x0, y0, z0), (x1, y0, z0), (x1, y1, z1), (x0, y1, z1)
    return [([a, b, c], cls, name, 0), ([a, c, d], cls, name, 0)]


def overhang(z_low):
    """A bumper face (y 0.30..0.6) standing 3.5 cm out over a face sloping back under it,
    from z 1.995 at y 0.30 to z_low at y 0.14: the bumper is a recess wall above it."""
    return quad(-0.5, 0.5, 0.30, 0.6, 2.03) + slope(-0.5, 0.5, 0.14, 0.30, z_low, 1.995)


def test_recess_search_rejects_a_face_under_an_overhang():
    # #179: the Golf GTI 16v Mk2's rear plate went under the bumper, facing the road
    # (tilt -0.73), and was invisible in game. Here the face under it tilts -0.52.
    host = P.HostPlate("front", (0.0, 0.2, 2.0), SIZE, (0, 0), 2)
    plate = P.plan_end(overhang(1.912), "front", host, 0.2, 0.0, 0.6)
    assert plate.tilt >= -0.4, plate


def test_recess_search_keeps_a_face_tilted_like_vanilla():
    # Facing down by 0.25 (14 degrees): within what vanilla plates do.
    host = P.HostPlate("front", (0.0, 0.2, 2.0), SIZE, (0, 0), 2)
    plate = P.plan_end(overhang(1.955), "front", host, 0.2, 0.0, 0.6)
    assert plate.source == "recess" and plate.surface == "bumperFa", plate
    assert -0.3 < plate.tilt < -0.2 and plate.centre[1] < 0.30, plate


def test_host_plate_facing_down_allows_its_tilt():
    # The vanilla Stratos's front plate faces down by 0.70: a port on it may too.
    host = P.HostPlate("front", (0.0, 0.2, 2.0), SIZE, (0, 0), 2, tilt=-0.7)
    plate = P.plan_end(overhang(1.912), "front", host, 0.2, 0.0, 0.6)
    assert plate.source == "recess" and plate.tilt < -0.5, plate


def curved_bumper(k, y0=0.0, y1=0.8, z=2.0, step=0.02):
    """A vertical bumper face curving back towards its sides: z = z - k x^2 (front)."""
    out = []
    xs = [-0.5 + step * i for i in range(int(round(1.0 / step)) + 1)]
    for x0, x1 in itertools.pairwise(xs):
        a, b = (x0, y0, z - k * x0 * x0), (x1, y0, z - k * x1 * x1)
        c, d = (x1, y1, z - k * x1 * x1), (x0, y1, z - k * x0 * x0)
        out += [([a, b, c], 1, "bumperFa", 0), ([a, c, d], 1, "bumperFa", 0)]
    return out


def test_fallback_plate_bends_round_a_curved_bumper():
    # #213: a flat plate pushed out to the bumper's centre stood off its sides by
    # k (w/2)^2 = 3.4 cm here; bent at its centre line (as vanilla plates on curved
    # bumpers are), its corners stand off by half that.
    host = P.HostPlate("front", (0.0, 0.4, 2.0), SIZE, (0, 0), 2)
    plate = P.plan_end(curved_bumper(2.0), "front", host, 0.4, 0.0, 0.6)
    assert plate.source == "host" and 0.1 < plate.bend <= P.BEND_MAX, plate
    w = SIZE[0] / 2
    for x in (-w, w):
        corner = plate.point(x, 0.0)
        surface = 2.0 - 2.0 * corner[0] ** 2
        assert 0 < corner[2] - surface < 0.017, (x, corner[2] - surface)


def test_plate_rests_flat_and_upright_on_a_flat_face():
    import numpy as np

    dm = front_map(quad(-0.5, 0.5, 0.0, 0.8, 2.0))
    rows, cols = slice(int(dm.row(0.46)), int(dm.row(0.34))), slice(int(dm.col(-0.13)), int(dm.col(0.13)))
    tilt, bend, d, gaps = P.rest(dm, rows, cols, (0.0, 0.4), np.isfinite(dm.depth[rows, cols]))
    assert (tilt, bend) == (0.0, 0.0) and abs(d + 2.0) < 1e-6 and gaps.max() < 1e-6


def test_bent_plate_section():
    # Two panels with the plate's UV split at its centre line, wound outwards.
    plate = P.Plate("rear", (0.0, 0.3), SIZE, 0.1, -2.0, "host", "bumperRa", bend=0.15)
    section = P.plate_section(plate, 3, [1])
    sub = section.subsections[0]
    assert len(sub.indices) == 12 and len(section.lod_vertices) == 6
    us = sorted({round(v.uv0[0], 4) for v in section.lod_vertices})
    assert us == [P.PLATE_UV[0], round((P.PLATE_UV[0] + P.PLATE_UV[2]) / 2, 4), P.PLATE_UV[2]]
    for t in range(0, 12, 3):
        a, b, c = (section.lod_vertices[i].position for i in sub.indices[t : t + 3])
        n = [
            (b[1] - a[1]) * (c[2] - a[2]) - (b[2] - a[2]) * (c[1] - a[1]),
            0,
            (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]),
        ]
        assert n[2] < 0, n  # rear plate: faces -z
    # The centre line is nearest the viewer (smallest d = z at the rear); the sides turn back.
    mid = [v.position[2] for v in section.lod_vertices if abs(v.position[0]) < 1e-9]
    side = [v.position[2] for v in section.lod_vertices if abs(v.position[0]) > 0.1]
    assert max(mid) < min(side) + 0.02 and min(side) > min(mid)


def test_fallback_hides_no_badge_where_it_can():
    # #241: a small emblem flush with the bumper at the host's height costs less to
    # cover (0.5 x ~1%) than the 7 cm it takes to clear it (0.1 per metre); a height
    # that hides nothing still comes first.
    tris = quad(-0.02, 0.02, 0.398, 0.408, 2.0, cls=4, feat=P.FEAT_BADGE) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    y, _, note = P.fallback_height(front_map(tris), 0.40, SIZE)
    assert y + SIZE[1] / 2 <= 0.40 or y - SIZE[1] / 2 >= 0.41, (y, note)
    assert "hides" not in note
