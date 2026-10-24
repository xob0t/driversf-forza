"""Checks for src/dsfforza/convert/cockpit.py that need no game files (a fraction of a second).

    uv run python tests/test_cockpit.py     # or every test: uv run python -m tests cockpit
"""

from __future__ import annotations

import sys
from pathlib import Path

from dsfforza.formats import carbin  # noqa: E402
from dsfforza.convert import cockpit  # noqa: E402
from dsfforza.convert import forza_materials as fm  # noqa: E402


def _display(transform: tuple, width: float = 0.25, height: float = 0.08) -> carbin.Section:
    """A flat digital_gauge panel (width x height m, two triangles) with UV0 0..1
    across it, like the S2000's cluster face."""
    corners = [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    verts = [carbin.Vertex((u * width, (1 - v) * height, 0.0), (u, v), (0.0, 0.0), (0.0, 0.0, 1.0))
             for u, v in corners]
    sub = carbin.SubSection("digital_gauge", 0, 4, transform, [0, 1, 2, 2, 1, 3])
    return carbin.Section("interior", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod0_vertices=verts, subsections=[sub])


def test_display_face_uses_the_second_transform():
    # S2000: the second transform maps the panel onto the dial artwork (0.79 x 0.24 of
    # the texture over a 0.25 x 0.08 m panel: about 3 UV units per metre, undistorted).
    labels, face = (0.126, 0.746, 0.301, 0.463), (0.009, 0.784, 0.008, 0.244)
    section = _display(labels + face)
    aniso, density = cockpit.uv_map_stats(section, section.subsections[0], face)
    assert abs(aniso - 1.0) < 0.05 and density > 2.5
    out, faces = cockpit.with_display_faces(section)
    assert faces == 2
    sub = out.subsections[0]
    assert sub.material == cockpit.DISPLAY_FACE and tuple(sub.uv_transform[:4]) == face
    assert section.subsections[0].material == "digital_gauge"   # the source is left alone
    assert fm.classify_cockpit(sub.material, "interior") == (fm.TEXTURED, fm.INTERIOR)
    m = fm.resolve(out, sub, fm.classify_cockpit)
    assert m.kind == "textured" and abs(m.bounds[2] - 0.793) < 1e-3


def test_blank_displays_stay_blank():
    labels = (0.126, 0.746, 0.301, 0.463)
    # A swatch (M5: scale 0) and a stretched strip (Raptor: 489 x 12 texels).
    for face in ((0.5, 0.0, 0.5, 0.0), (0.02, 0.95, 0.4, 0.023)):
        section = _display(labels + face)
        assert cockpit.display_face(section, section.subsections[0]) is None
        assert cockpit.with_display_faces(section) == (section, 0)


def test_badges_follow_their_section():
    # Cabin badges (horn logo, labels) sample interior_LOD0; badges on the parts
    # seen through the windscreen (bonnet decals, mirror badges) the exterior atlas.
    assert fm.classify_cockpit("badge", "steering_wheel") == (fm.TEXTURED, fm.INTERIOR)
    assert fm.classify_cockpit("badge", "interior") == (fm.TEXTURED, fm.INTERIOR)
    for section in ("hooda", "a_pillar", "mirrorL"):
        assert fm.classify_cockpit("badge", section) == (fm.TEXTURED, fm.NODAMAGE)
        assert fm.classify_cockpit("stripe_badge", section) == (fm.TEXTURED, fm.NODAMAGE)
        assert fm.classify_cockpit("gauge_emissive", section) == (fm.TEXTURED, fm.INTERIOR)
    assert fm.classify_cockpit("liveryBadge", "hooda")[0] == fm.SKIP
    assert fm.classify_cockpit("badge") == (fm.TEXTURED, fm.INTERIOR)   # role-only callers


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
