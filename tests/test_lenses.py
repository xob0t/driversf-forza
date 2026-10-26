"""lenses.bake (Forza lamp lenses baked over what lies behind them) and the red lamp
curve's knot spacing, on synthetic sections and atlases.

uv run python tests/test_lenses.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import itertools
import tempfile
from pathlib import Path

from dsfforza.convert import dsf_materials as dm
from dsfforza.convert import forza_materials as fm
from dsfforza.convert import lenses
from dsfforza.formats import carbin

SIZE = 32
LENS = (200, 0, 0, 128)  # a red lens, half opaque
LAMP = (255, 255, 255, 200)  # a white reflector with a glow mask


class _Atlases:
    """fm.AtlasCache stand-in: the Lights atlas's left half lens texels, right half lamp."""

    def __init__(self):
        rgba = bytearray()
        for _ in range(SIZE):
            for x in range(SIZE):
                rgba += bytes(LENS if x < SIZE // 2 else LAMP)
        self.lights = (SIZE, SIZE, rgba)

    def get(self, family):
        assert family == fm.LIGHTS
        return self.lights

    def texel(self, x, y):
        o = (y * SIZE + x) * 4
        return tuple(self.lights[2][o : o + 4])


def _quad(name: str, material: str, z: float, u0: float, u1: float, x: float = 0.0) -> carbin.Section:
    pts = [(x - 0.1, 0.4, z), (x + 0.1, 0.4, z), (x - 0.1, 0.6, z), (x + 0.1, 0.6, z)]
    uvs = [(u0, 0.0), (u1, 0.0), (u0, 1.0), (u1, 1.0)]
    verts = [carbin.Vertex(p, uv, (0, 0), (0.0, 0.0, 1.0)) for p, uv in zip(pts, uvs)]
    sub = carbin.SubSection(material, 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, [0, 1, 2, 2, 1, 3])
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def _bake(sections, lens_sections):
    atlases = _Atlases()
    ids = {id(sub) for s in lens_sections for sub in s.subsections}
    with tempfile.TemporaryDirectory() as car_dir:
        report = lenses.bake(sections, {1}, atlases, ids, Path(car_dir))
    return atlases, report


def test_lens_over_lamp():
    lens = _quad("glassLHL", "detail_glass_red", 2.0, 0.0, 0.5)
    lamp = _quad("headlightL", "textured_reflector", 1.9, 0.5, 1.0)
    atlases, report = _bake([lens, lamp], [lens])
    a = LENS[3] / 255
    want = tuple(round(a * l + (1 - a) * b) for l, b in zip(LENS, LAMP))
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got, want)), (got, want)
    assert report["glassLHL/detail_glass_red"]["behind"] == 1.0
    assert atlases.texel(SIZE * 3 // 4, SIZE // 2) == LAMP  # the lamp's own texels stay


def test_nothing_behind_keeps_the_lens():
    lens = _quad("glassLHL", "detail_glass_clear", 2.0, 0.0, 0.5)
    far = _quad("headlightL", "textured_reflector", 2.0 - 2 * lenses.RAY_DEPTH, 0.5, 1.0)
    atlases, report = _bake([lens, far], [lens])
    assert atlases.texel(SIZE // 4, SIZE // 2) == LENS
    assert report["glassLHL/detail_glass_clear"]["behind"] == 0.0


def test_texels_other_triangles_sample_stay():
    # A reflector that samples the lens's texels too: they keep their colour.
    lens = _quad("glassLHL", "detail_glass_red", 2.0, 0.0, 0.5)
    lamp = _quad("headlightL", "textured_reflector", 1.9, 0.0, 0.5)
    atlases, report = _bake([lens, lamp], [lens])
    assert atlases.texel(SIZE // 4, SIZE // 2) == LENS
    assert report["glassLHL/detail_glass_red"]["shared"] > 0


def test_lens_techniques():
    assert lenses.is_lens_technique("detail_glass_clear")
    assert lenses.is_lens_technique("detail_glass_amber")
    assert not lenses.is_lens_technique("lights_gls_tail_light")
    assert not lenses.is_lens_technique("glass")


def test_glass_max_opacity():
    xml = (
        '<ShaderSettings>\n  <lights_glass>\n    <GlassMaxOpacity value="0.75"/>\n  </lights_glass>\n'
        '  <detail_glass_red>\n    <DarkValues value="0.5"/>\n  </detail_glass_red>\n'
        '  <glass><GlassMaxOpacity value="3.3" /></glass>\n</ShaderSettings>\n'
    )
    with tempfile.TemporaryDirectory() as car_dir:
        (Path(car_dir) / "ShaderSettings.xml").write_text(xml)
        assert lenses.glass_max_opacity(Path(car_dir)) == {"lights_glass": 0.75, "glass": 1.0}


def test_red_curve_knots_apart():
    # SUB_WRXSTi_04: most red lamp texels are one flat lens; its DXT noise must not
    # spread over the whole alpha range.
    scores = [0.3 + 0.001 * (k % 50) for k in range(400)] + [0.69 + 0.0001 * (k % 30) for k in range(1200)]
    xs, _ = dm.red_lamp_curve(scores)
    assert min(b - a for a, b in itertools.pairwise(xs)) >= dm.RED_CURVE_MIN_GAP - 1e-9
    lo = dm._interp(0.690, xs, dm._LAMP_Y_RED)
    hi = dm._interp(0.693, xs, dm._LAMP_Y_RED)
    assert hi - lo < 10, (lo, hi)


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"{name}: ok")
