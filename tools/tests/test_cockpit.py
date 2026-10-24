"""Checks for tools/cockpit.py that need no game files (a fraction of a second).

    python tools/tests/test_cockpit.py     # or every test: python -m tools.tests cockpit
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import carbin  # noqa: E402
import cockpit  # noqa: E402
import forza_materials as fm  # noqa: E402


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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
