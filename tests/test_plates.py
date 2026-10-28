"""Licence plate placement (src/dsfforza/convert/plates.py): the fallback plate
with no face to sit on (step 3) keeps off badges, lamps and section boundaries
(#159: the Mazda 2's front plate over its emblem and bonnet edge)."""

from __future__ import annotations

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
    y, note = P.fallback_height(dm, 0.40, SIZE)
    assert abs(y - 0.40) < 0.015
    assert note == "no flat face of plate size"


def test_plate_keeps_off_a_badge():
    # An emblem 1 cm proud of the bumper, at the host's plate height.
    tris = quad(-0.08, 0.08, 0.35, 0.45, 2.01, cls=4, feat=P.FEAT_BADGE) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    dm = front_map(tris)
    y, _ = P.fallback_height(dm, 0.40, SIZE)
    # Clear of the badge: the plate's edge beyond the emblem's.
    assert y + SIZE[1] / 2 <= 0.355 or y - SIZE[1] / 2 >= 0.445, y


def test_plate_keeps_off_a_lamp():
    tris = quad(-0.3, 0.3, 0.30, 0.42, 2.005, cls=4, feat=P.FEAT_LAMP) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    y, _ = P.fallback_height(front_map(tris), 0.36, SIZE)
    assert y + SIZE[1] / 2 <= 0.305 or y - SIZE[1] / 2 >= 0.415, y


def test_plate_stays_below_the_bonnet_shut_line():
    # Bumper face up to y 0.45, the bonnet above it a little further back.
    tris = quad(-0.5, 0.5, 0.0, 0.45, 2.0) + quad(-0.5, 0.5, 0.45, 0.8, 1.995, name="hooda")
    y, _ = P.fallback_height(front_map(tris), 0.44, SIZE)
    assert y + SIZE[1] / 2 <= 0.455, y


def test_plate_may_cross_a_grille():
    # A grille mesh across the whole bumper centre: a plate goes on it (bracket),
    # at the host's height.
    tris = quad(-0.3, 0.3, 0.1, 0.6, 2.0, cls=2, feat=P.FEAT_GRILLE) + quad(-0.5, 0.5, 0.0, 0.8, 2.01)
    y, _ = P.fallback_height(front_map(tris), 0.35, SIZE)
    assert abs(y - 0.35) < 0.015


def test_badge_band_is_grille_artwork():
    # A badge-technique band across the whole nose (the BMW M1's kidneys and grille
    # strip) is no emblem: the plate stays at the host's height on it.
    tris = quad(-0.45, 0.45, 0.30, 0.50, 2.002, cls=4, feat=P.FEAT_BADGE) + quad(-0.5, 0.5, 0.0, 0.8, 2.0)
    y, _ = P.fallback_height(front_map(tris), 0.40, SIZE)
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
