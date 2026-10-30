"""lenses.bake (Forza lamp lenses baked over what lies behind them) and the red lamp
curve's knot spacing, on synthetic sections and atlases.

uv run python tests/test_lenses.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import dataclasses
import itertools
import tempfile
from pathlib import Path

import numpy as np

from dsfforza.convert import dsf_materials as dm
from dsfforza.convert import forza_materials as fm
from dsfforza.convert import lenses
from dsfforza.formats import carbin

SIZE = 32
LENS = (200, 0, 0, 128)  # a red lens, half opaque
LAMP = (255, 255, 255, 200)  # a white reflector with a glow mask


def _filtered(lens, behind, technique, settings=None):
    """What Forza shows of `behind` through `lens` (bytes), and the share of its glow it passes."""
    f = lenses.lens_filter(technique, np.array(lens[:3], float), settings or {})
    rgb = [round(b * f[c] ** 0.5) for c, b in enumerate(behind[:3])]
    return rgb, float(f[int(np.argmax(rgb))])


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


def _quad(
    name: str, material: str, z: float, u0: float, u1: float, x: float = 0.0, normal=(0.0, 0.0, 1.0)
) -> carbin.Section:
    pts = [(x - 0.1, 0.4, z), (x + 0.1, 0.4, z), (x - 0.1, 0.6, z), (x + 0.1, 0.6, z)]
    uvs = [(u0, 0.0), (u1, 0.0), (u0, 1.0), (u1, 1.0)]
    verts = [carbin.Vertex(p, uv, (0, 0), normal) for p, uv in zip(pts, uvs)]
    sub = carbin.SubSection(material, 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, [0, 1, 2, 2, 1, 3])
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def _bake(sections, lens_sections, xml: str | None = None, dropped: set | None = None):
    atlases = _Atlases()
    ids = {id(sub) for s in lens_sections for sub in s.subsections}
    with tempfile.TemporaryDirectory() as car_dir:
        if xml:
            (Path(car_dir) / "ShaderSettings.xml").write_text(xml)
        report = lenses.bake(sections, {1}, atlases, ids, Path(car_dir), dropped=dropped)
    return atlases, report


def test_lens_over_lamp():
    lens = _quad("glassLHL", "detail_glass_red", 2.0, 0.0, 0.5)
    lamp = _quad("headlightL", "textured_reflector", 1.9, 0.5, 1.0)
    atlases, report = _bake([lens, lamp], [lens])
    # Forza's lens is a filter: the lamp behind through it, never the lens's own colour or alpha
    # (research/forza-lenses-re.md).
    rgb, through = _filtered(LENS, LAMP, "detail_glass_red")
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    # The glow is the lamp's, through the filter.
    assert abs(dm.lamp_score(*got) - dm.lamp_score(*LAMP) * through) < 0.01, (got, LAMP, through)
    assert report["glassLHL/detail_glass_red"]["behind"] == 1.0
    assert atlases.texel(SIZE * 3 // 4, SIZE // 2) == LAMP  # the lamp's own texels stay


def test_nothing_behind_keeps_the_lens():
    lens = _quad("glassLHL", "detail_glass_clear", 2.0, 0.0, 0.5)
    far = _quad("headlightL", "textured_reflector", 2.0 - 2 * lenses.RAY_DEPTH, 0.5, 1.0)
    atlases, report = _bake([lens, far], [lens])
    assert atlases.texel(SIZE // 4, SIZE // 2) == LENS[:3] + (0,)  # its own texel, unlit
    assert report["glassLHL/detail_glass_clear"]["behind"] == 0.0


def test_texels_other_triangles_sample_stay():
    # A reflector that samples the lens's texels too: they keep their colour.
    lens = _quad("glassLHL", "detail_glass_red", 2.0, 0.0, 0.5)
    lamp = _quad("headlightL", "textured_reflector", 1.9, 0.0, 0.5)
    atlases, report = _bake([lens, lamp], [lens])
    assert atlases.texel(SIZE // 4, SIZE // 2) == LENS
    assert report["glassLHL/detail_glass_red"]["shared"] > 0


def test_swatch_lens_keeps_its_tint():
    # A lens stretched over a tint swatch (2 texels across a 20 cm lens, the X5 M's red tail
    # glass, issue #141): its texels stand for lens points far apart and keep the lens's own
    # colour instead of one random part of the lamp behind.
    inner = _quad("glassLTL", "detail_glass_red", 2.0, 0.0, 2 / SIZE, x=0.5)
    outer = _quad("trunk", "detail_glass_red", 2.0, 0.0, 2 / SIZE, x=0.8)
    lamps = [_quad(f"taillight{i}", "textured_reflector", 1.9, 0.5, 1.0, x=x) for i, x in enumerate((0.5, 0.8))]
    atlases, report = _bake([inner, outer, *lamps], [inner, outer])
    assert atlases.texel(0, SIZE // 2) == LENS
    assert atlases.texel(1, SIZE // 2) == LENS
    assert report["glassLTL/detail_glass_red"]["scrambled"] > 0
    assert report["glassLTL/detail_glass_red"]["texels"] == 0
    # The same lens over its own texels is baked as before.
    lens = _quad("glassLTL", "detail_glass_red", 2.0, 0.0, 0.5, x=0.5)
    atlases, report = _bake([lens, lamps[0]], [lens])
    assert atlases.texel(SIZE // 4, SIZE // 2) != LENS
    assert "scrambled" not in report["glassLTL/detail_glass_red"]


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


def _grid(name: str, material: str, z, u0: float, u1: float, n: int = 8, sign: float = -1.0) -> carbin.Section:
    """An n x n grid 0.2 m wide facing Forza's front (-z: sign -1) or rear; z(x, y) gives
    each vertex's z (a reflector bowl)."""
    verts, idx = [], []
    for j in range(n + 1):
        for i in range(n + 1):
            x, y = -0.1 + 0.2 * i / n, 0.4 + 0.2 * j / n
            uv = (u0 + (u1 - u0) * i / n, j / n)
            verts.append(carbin.Vertex((x, y, z(x, y)), uv, (0, 0), (0.0, 0.0, sign)))
    for j in range(n):
        for i in range(n):
            a = j * (n + 1) + i
            idx += [a, a + 1, a + n + 1, a + n + 1, a + 1, a + n + 2]
    sub = carbin.SubSection(material, 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, idx)
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


class _ClearAtlases(_Atlases):
    """A clear lens (half opaque, its own mask low) left, a reflector with a flat mask 255 right."""

    def __init__(self, lens=(180, 180, 180, 100), lamp=(200, 200, 200, 255)):
        rgba = bytearray()
        for _ in range(SIZE):
            for x in range(SIZE):
                rgba += bytes(lens if x < SIZE // 2 else lamp)
        self.lights = (SIZE, SIZE, rgba)

    def get(self, family):
        return self.lights  # swatch trims (nodamage) sample it too


def _bake_glow(lens, lamp, atlases=None):
    atlases = atlases or _ClearAtlases()
    glow = lenses.new_glow(SIZE, SIZE)
    with tempfile.TemporaryDirectory() as car_dir:
        ids = {id(s) for s in lens.subsections}
        lenses.bake([lens, lamp], {1}, atlases, ids, Path(car_dir), glow=glow)
    assert (np.isfinite(glow[..., 0]) == np.isfinite(glow[..., 1])).all()
    return atlases, glow[..., 0]


def test_head_lamp_glow_follows_the_reflector_bowl():
    # Issue #124: a clear front lens over a reflector whose mask is 255 everywhere (the Lotus
    # Carlton's) gets its raw glow from the bowl: brightest over its deepest part, dim at the
    # rim; master gave every texel the lens's own score.
    lens = _grid("glassLHL", "detail_glass_clear", lambda x, y: -2.0, 0.0, 0.5)
    bowl = _grid("headlightL", "head_light", lambda x, y: -2.0 + 0.1 - 4.5 * (x * x + (y - 0.5) ** 2), 0.5, 1.0)
    _, raw = _bake_glow(lens, bowl)
    assert np.isfinite(raw[:, : SIZE // 2]).all()
    assert np.isnan(raw[:, SIZE // 2 :]).all()  # the reflector's own texels: lamp_glow's
    centre, rim = raw[SIZE // 2, SIZE // 4], raw[1, 1]
    # through the lens's filter (its red texel passes 0.65 of a white lamp's brightest channel)
    assert centre > 120 and rim < centre / 3, (centre, rim)


def test_day_glow_on_vanilla_distribution_with_a_dark_rim():
    # A round lamp with a flat raw glow, seen straight on (one texel per view sample): the rim
    # fades, so the middle glows and the edge doesn't, on vanilla's day-lit distribution.
    n, step = 40, 0.005
    yy, xx = np.mgrid[0:n, 0:n]
    disc = (yy - n / 2 + 0.5) ** 2 + (xx - n / 2 + 0.5) ** 2 < 15**2
    glow = lenses.new_glow(n, n)
    glow[disc, 0], glow[disc, 1] = 255.0, 0.0
    glow[..., 2], glow[..., 3] = (xx + 0.5) * step, (yy + 0.5) * step
    out = lenses.day_glow(glow, (np.nonzero(disc), disc, (0.0, 0.0), step))
    assert np.isnan(out[~disc]).all()
    centre, edge = out[n // 2, n // 2], out[n // 2, n // 2 - 14]
    assert centre > 100 and centre == np.nanmax(out) and edge < 20, (centre, edge)
    assert 30 < np.median(out[disc]) < 90, np.median(out[disc])
    # Too small a lamp keeps its converted alpha.
    small = disc & (xx < 3 + n // 2 - 15)
    assert lenses.day_glow(glow, (np.nonzero(small), small, (0.0, 0.0), step)) is None


def test_day_glow_follows_the_lamps_picture():
    # #286: a flat raw glow over a lamp whose picture has bright stripes with dark ones between
    # (a fluted lens, as vanilla's lamps glow): the bright stripes rank high, the dark ones low,
    # not one smooth blob.
    n, step = 30, 0.004
    yy, xx = np.mgrid[0:n, 0:n]
    disc = (yy - n / 2 + 0.5) ** 2 + (xx - n / 2 + 0.5) ** 2 < 14**2
    glow = lenses.new_glow(n, n)
    glow[disc, 0], glow[disc, 1] = 255.0, 0.0
    glow[..., 2], glow[..., 3] = (xx + 0.5) * step, (yy + 0.5) * step
    colour = np.zeros((n, n, 4))
    colour[..., :3] = np.where((xx // 2 % 2 == 0)[..., None], 220.0, 80.0)
    out = lenses.day_glow(glow, (np.nonzero(disc), disc, (0.0, 0.0), step), colour)
    middle = disc & ((yy - n / 2) ** 2 + (xx - n / 2) ** 2 < 6**2)
    bright, dark = middle & (xx // 2 % 2 == 0), middle & (xx // 2 % 2 == 1)
    assert np.mean(out[bright]) > 2 * np.mean(out[dark]), (np.mean(out[bright]), np.mean(out[dark]))
    # Without the picture the same lamp is smooth there.
    flat = lenses.day_glow(glow, (np.nonzero(disc), disc, (0.0, 0.0), step))
    assert abs(np.mean(flat[bright]) - np.mean(flat[dark])) < 0.2 * np.mean(flat[middle])


def test_head_lamp_surface_glow():
    # A head lamp reflector seen directly (no lens, or behind DSF's clear glass): its mask times
    # how deep each point sits, so the bowl's centre glows and its rim doesn't.
    bowl = _grid("headlightL", "head_light", lambda x, y: -1.9 - 4.5 * (x * x + (y - 0.5) ** 2), 0.5, 1.0)
    glow = lenses.new_glow(SIZE, SIZE)
    assert lenses.lamp_glow([bowl], {1}, _ClearAtlases(), glow) > 0
    raw = glow[..., 0]
    assert set(np.unique(glow[..., 1][np.isfinite(raw)])) == {0.0}
    assert np.isnan(raw[:, : SIZE // 2]).all()
    assert raw[SIZE // 2, SIZE * 3 // 4] > 200 and raw[1, SIZE // 2 + 1] < 60, raw
    # Already set texels (a lens's) stay.
    glow[..., 0] = 7.0
    assert lenses.lamp_glow([bowl], {1}, _ClearAtlases(), glow) == 0 and (glow[..., 0] == 7.0).all()


def test_head_lamp_glow_only_on_clear_front_lenses():
    bowl = lambda x, y: -2.0 + 0.1 - 4.5 * (x * x + (y - 0.5) ** 2)  # noqa: E731
    lens = _grid("glassLHL", "detail_glass_clear", lambda x, y: -2.0, 0.0, 0.5)
    # A red lens keeps its lamp score (the coloured lenses' glow is unchanged).
    _, glow = _bake_glow(lens, _grid("headlightL", "head_light", bowl, 0.5, 1.0), _ClearAtlases(lens=(200, 0, 0, 128)))
    assert np.isnan(glow).all()
    # A rear lens (Forza +z) too.
    rear = _grid("glassLTL", "detail_glass_clear", lambda x, y: 2.0, 0.0, 0.5, sign=1.0)
    _, glow = _bake_glow(rear, _grid("taillightL", "reverse_light", lambda x, y: 1.9, 0.5, 1.0, sign=1.0))
    assert np.isnan(glow).all()
    # A lens with no lamp behind it (only a trim housing) keeps its own glow.
    _, glow = _bake_glow(lens, _grid("headlightL", "frame", bowl, 0.5, 1.0))
    assert np.isnan(glow).all()
    # A chrome reflector bowl is a lamp (the Honda Civic Si 99's).
    _, glow = _bake_glow(lens, _grid("headlightL", "chrome", bowl, 0.5, 1.0))
    assert np.isfinite(glow[:, : SIZE // 2]).all()


def test_render_takes_the_lens_glow():
    t = dm.Texturing(Path(), size=(32, 32))
    t.families = (fm.LIGHTS,)
    src = bytearray(bytes((180, 180, 180, 100)) * (SIZE * SIZE))

    class Atl:
        def get(self, family):
            return SIZE, SIZE, src

    t.atlases = Atl()
    t.regions = {fm.LIGHTS: [dm.Region(fm.LIGHTS, (8, 4, 16, 12))]}
    t.regions[fm.LIGHTS][0].dst, t.regions[fm.LIGHTS][0].size = (0, 0), (8 + 2 * dm.PAD, 8 + 2 * dm.PAD)
    t.scale = {fm.LIGHTS: 1.0}
    t.lamp_raw = lenses.new_glow(SIZE, SIZE)
    t._lamp_glow, t._glow_done = np.full((SIZE, SIZE), np.nan), True
    t._lamp_glow[:, 8:12] = 200.0
    out = np.frombuffer(bytes(t.render()), np.uint8).reshape(32, 32, 4)
    plain = t._convert_texel(fm.LIGHTS, 180, 180, 180, 100)[3]
    assert plain != 200
    assert out[dm.PAD + 2, dm.PAD + 1, 3] == 200  # source x 9: the glow
    assert out[dm.PAD + 2, dm.PAD + 6, 3] == plain  # source x 14: no glow, the converted alpha


def test_red_curve_knots_apart():
    # SUB_WRXSTi_04: most red lamp texels are one flat lens; its DXT noise must not
    # spread over the whole alpha range.
    scores = [0.3 + 0.001 * (k % 50) for k in range(400)] + [0.69 + 0.0001 * (k % 30) for k in range(1200)]
    xs, _ = dm.red_lamp_curve(scores)
    assert min(b - a for a, b in itertools.pairwise(xs)) >= dm.RED_CURVE_MIN_GAP - 1e-9
    lo = dm._interp(0.690, xs, dm._LAMP_Y_RED)
    hi = dm._interp(0.693, xs, dm._LAMP_Y_RED)
    assert hi - lo < 10, (lo, hi)


def test_opacity_rules():
    # Lamp glass: Forza's opacity starts at 1, so it is GlassMaxOpacity. Covers: max(texel
    # alpha, GlassColor.w). Lenses have none (they filter).
    settings = {
        "lights_gls_tail_light": {"GlassMaxOpacity": 0.8},
        "lights_glass": {"GlassMaxOpacity": 0.9, "GlassColor": (0.5, 0.5, 0.5, 0.25)},
    }
    assert lenses.opacity_rule("detail_glass_clear", settings) == (0.0, 1.0)
    assert lenses.opacity_rule("lights_gls_tail_light", settings) == (1.0, 0.8)
    assert lenses.opacity_rule("lights_gls_noemit", {}) == (1.0, 1.0)
    assert lenses.opacity_rule("lights_glass", settings) == (0.25, 0.9)
    assert lenses.opacity_rule("lights_glass", {}) == (0.0, 1.0)
    assert lenses.is_glass_technique("lights_gls_oldhead") and lenses.is_glass_technique("lights_glass")
    # The red lens filter: red remapped by DarkValues / LightValues, in Forza's square space.
    red = {"detail_glass_red": {"DarkValues": 0.4, "LightValues": 1.5}}
    f = lenses.lens_filter("detail_glass_red", (155, 0, 2), red)
    assert abs(f[0] - (0.4 + 1.1 * (155 / 255) ** 2)) < 1e-9 and f[1] == 0.0
    # Clear and amber: the texel plus GlassColor (shared 0.2 grey on clear), its RGB converted
    # from sRGB to linear as Forza uploads it (0.2 -> 0.033), at most 1.
    grey = ((0.2 + 0.055) / 1.055) ** 2.4
    f = lenses.lens_filter("detail_glass_clear", (128, 255, 0), {})
    assert np.allclose(f, ((128 / 255) ** 2 + grey, 1.0, grey))
    f = lenses.lens_filter("detail_glass_amber", (0, 0, 0), {"detail_glass_amber": {"GlassColor": (0.5, 0.0, 0.0, 1)}})
    assert np.allclose(f, (((0.5 + 0.055) / 1.055) ** 2.4, 0.0, 0.0))
    assert not lenses.emits("lights_glass") and lenses.emits("lights_gls_reverse_light")
    assert not lenses.emits("lights_gls_noemit") and not lenses.emits("detail_glass_red")


def test_lamp_glass_at_its_max_opacity():
    # lights_gls_* glass over a reflector: GlassMaxOpacity of it shows, whatever its texel alpha.
    xml = (
        '<ShaderSettings><lights_gls_tail_light><GlassMaxOpacity value="0.8"/></lights_gls_tail_light></ShaderSettings>'
    )
    glass = _quad("glassLTL", "lights_gls_tail_light", 2.0, 0.0, 0.5)
    lamp = _quad("taillightL", "textured_reflector", 1.9, 0.5, 1.0)
    atlases, _ = _bake([glass, lamp], [glass], xml)
    rgb = [round(0.8 * a + 0.2 * b) for a, b in zip(LENS[:3], LAMP[:3])]
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    assert abs(dm.lamp_score(*got) - dm.lamp_score(*LENS)) < 0.01  # its own glow


def test_cover_passes_the_lamp_glow():
    # A cover (lights_glass) has no glow of its own: (1 - opacity)^2 of the lamp's behind it
    # passes (Forza's blend in its square light space, research/forza-lenses-re.md section 7).
    cover = _quad("glassLTL", "lights_glass", 2.0, 0.0, 0.5)
    lamp = _quad("taillightL", "textured_reflector", 1.9, 0.5, 1.0)
    atlases, _ = _bake([cover, lamp], [cover])
    got = atlases.texel(SIZE // 4, SIZE // 2)
    want = dm.lamp_score(*LAMP) * (1 - LENS[3] / 255) ** 2
    assert abs(dm.lamp_score(*got) - want) < 0.01, (got, dm.lamp_score(*got), want)


def test_swatch_lens_tints_the_lamp_behind():
    # A swatch lens (issue #145) with dropped= given: left out, its tint laid over the reflector.
    inner = _quad("glassLTL", "detail_glass_red", 2.0, 0.0, 2 / SIZE, x=0.5)
    outer = _quad("trunk", "detail_glass_red", 2.0, 0.0, 2 / SIZE, x=0.8)
    lamps = [_quad(f"taillight{i}", "textured_reflector", 1.9, 0.5, 1.0, x=x) for i, x in enumerate((0.5, 0.8))]
    dropped = set()
    atlases, report = _bake([inner, outer, *lamps], [inner, outer], dropped=dropped)
    assert dropped == {("glassLTL", "detail_glass_red"), ("trunk", "detail_glass_red")}
    rgb = [round(b * l / 255) for l, b in zip(LENS[:3], LAMP[:3])]  # its filter: the texel
    got = atlases.texel(SIZE * 3 // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    assert got[3] == LAMP[3]  # the reflector keeps its glow mask
    assert report["tinted"]["texels"] == (SIZE // 2) * SIZE


def test_clear_swatch_lens_over_a_lamp_is_left_out():
    # Issue #250: a clear swatch lens over a whole head lamp unit (the Hennessey Venom GT's lexan
    # covers), whose rays meet the housing more than the lamp, is left out with its tint on the
    # lamp it covers; kept, it was an opaque grey plate that glowed white by day. A red one
    # stays the lamp surface (a tail lens over a small bulb).
    front = (0.0, 0.0, -1.0)
    for colour, left_out in (((180, 180, 180, 128), True), (LENS, False)):
        sections, ids = [], set()
        for i, x in enumerate((0.5, 0.8)):  # two lenses on the same swatch texels
            lens = _quad(f"lexan{i}", "detail_glass_clear", -2.0, 0.0, 2 / SIZE, x=x, normal=front)
            bulb = _quad(f"headlight{i}", "head_light", -1.9, 0.5, 1.0, x=x - 0.15, normal=front)  # a quarter
            housing = _quad(f"headlight{i}", "frame", -1.9, 0.5, 1.0, x=x + 0.05, normal=front)
            sections += [lens, bulb, housing]
            ids.add(id(lens.subsections[0]))
        atlases = _ClearAtlases(lens=colour)
        dropped = set()
        with tempfile.TemporaryDirectory() as car_dir:
            report = lenses.bake(sections, {1}, atlases, ids, Path(car_dir), dropped=dropped)
        assert report["lexan0/detail_glass_clear"]["scrambled"] > 0
        assert (("lexan0", "detail_glass_clear") in dropped) == left_out, (colour, dropped)


def test_flat_lens_is_baked_as_a_filter():
    # A lens of one flat colour (the BMW X6 M's red tail glass, 40,000 texels of (183, 0, 0, 118))
    # is kept and baked like any lens: the lamp behind through its filter, the lamp's glow
    # through it (#145 left the rear ones out; Forza filters whatever lies behind, #340).
    for z, normal, name in ((2.0, (0.0, 0.0, 1.0), "glassLTL"), (-2.0, (0.0, 0.0, -1.0), "glassLHL")):
        lens = _quad(name, "detail_glass_red", z, 0.0, 0.5, normal=normal)
        lamp = _quad("lampL", "textured_reflector", z - 0.1 if z > 0 else z + 0.1, 0.5, 1.0, normal=normal)
        dropped = set()
        atlases, _ = _bake([lens, lamp], [lens], dropped=dropped)
        assert not dropped
        rgb, through = _filtered(LENS, LAMP, "detail_glass_red")
        got = atlases.texel(SIZE // 4, SIZE // 2)
        assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (z, got, rgb)
        assert abs(dm.lamp_score(*got) - dm.lamp_score(*LAMP) * through) < 0.01, (z, got)


def test_laid_back_front_lens_sees_the_lamp_along_the_axis():
    # A front lens facing up (the Panoz Esperante's head lamps): along its normal nothing, along
    # the car's axis the lamp behind it.
    lens = _quad("glassLHL", "detail_glass_clear", -2.0, 0.0, 0.5, normal=(0.0, 0.95, -0.31))
    lamp = _quad("headlightL", "textured_reflector", -1.9, 0.5, 1.0, normal=(0.0, 0.0, -1.0))
    atlases, report = _bake([lens, lamp], [lens])
    rgb, _ = _filtered(LENS, LAMP, "detail_glass_clear")
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    assert report["glassLHL/detail_glass_clear"]["behind"] == 1.0
    # A lens facing sideways or straight up (a side marker) doesn't look along the axis.
    lens = _quad("glassLHL", "detail_glass_clear", -2.0, 0.0, 0.5, normal=(0.0, 1.0, 0.0))
    _, report = _bake([lens, lamp], [lens])
    assert report["glassLHL/detail_glass_clear"]["behind"] == 0.0


def test_laid_back_rear_lens_sees_the_lamp_along_the_axis():
    # The same at the rear (issue #189): into the car is -z there.
    lens = _quad("glassLTL", "detail_glass_clear", 2.0, 0.0, 0.5, normal=(0.0, 0.95, 0.31))
    lamp = _quad("taillightL", "textured_reflector", 1.9, 0.5, 1.0, normal=(0.0, 0.0, 1.0))
    atlases, report = _bake([lens, lamp], [lens])
    rgb, _ = _filtered(LENS, LAMP, "detail_glass_clear")
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    assert report["glassLTL/detail_glass_clear"]["axis"] > 0


def test_cover_over_its_lenses_is_left_out():
    # A lamp cover drawn with its lenses' texels (the Panoz Esperante's tail covers): left out,
    # not baked into the lenses' texels.
    red = _quad("glassLTL", "detail_glass_red", 2.0, 0.0, 0.5)
    cover = _quad("glassLTL", "lights_glass", 2.01, 0.0, 0.5)
    own = _quad("glassRTL", "lights_glass", 2.01, 0.5, 1.0, x=0.5)  # texels of its own: baked
    t = dm.Texturing(Path())
    t.atlases = _Atlases()
    sections = [red, cover, own]
    ids = set()
    for section in sections:
        for sub in section.subsections:
            t.resolved[(id(section), id(sub))] = _textured(section, sub)
            ids.add(id(sub))
    t._drop_lens_skins(sections, {1}, ids)
    assert t.dropped_lenses == {("glassLTL", "lights_glass")}
    assert ids == {id(red.subsections[0]), id(own.subsections[0])}


def _textured(section, sub):

    return dataclasses.replace(fm.resolve(section, sub), role=fm.TEXTURED, family=fm.LIGHTS)


def test_tiling_lamp_part_unwrapped():
    # The Mustang GT 05's head lamp bowls: every triangle inside one tile (here tile 3).
    bowl = _quad("body", "textured_reflector", -1.0, 3.1, 3.4)
    m = fm.resolve(bowl, bowl.subsections[0])
    assert m.kind == "tiling"
    got = dm.unwrap_tiles(bowl, bowl.subsections[0], m)
    assert got is not None and got.kind == "textured"
    pool = bowl.vertices_for(bowl.subsections[0])
    us = sorted({round(got.uv(v.uv0)[0], 6) for v in pool})
    assert us == [0.1, 0.4], us
    assert abs(got.bounds[0] - 0.1) < 1e-6 and abs(got.bounds[2] - 0.4) < 1e-6
    # Triangles mostly across a tile edge can't be laid on the atlas: one flat colour.
    edge = _strip([2.6, 3.4], (-0.4, 0.4))
    assert dm.unwrap_tiles(edge, edge.subsections[0], fm.resolve(edge, edge.subsections[0])) is None
    # A little past the edge (a fifth of the quad's UV area): clamped into the tile.
    edge = _quad("body", "textured_reflector", -1.0, 2.9, 3.4)
    got = dm.unwrap_tiles(edge, edge.subsections[0], fm.resolve(edge, edge.subsections[0]))
    assert got is not None
    raw = [[edge.lod_vertices[i].uv0 for i in t] for t in edge.subsections[0].triangles()]
    assert [round(u, 6) for u, _ in got.triangle(raw[0])] == [0.0, 0.4, 0.0]
    # One of many is clamped into its tile.
    sections = [_quad("body", "textured_reflector", -1.0, 3.1 + 0.01 * k, 3.2 + 0.01 * k) for k in range(10)]
    sections.append(_quad("body", "textured_reflector", -1.0, 3.9, 4.05))
    verts, idx = [], []
    for q in sections:
        idx += [len(verts) + i for i in q.subsections[0].indices]
        verts += q.lod_vertices
    sub = carbin.SubSection("textured_reflector", 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, idx)
    many = carbin.Section("body", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])
    got = dm.unwrap_tiles(many, sub, fm.resolve(many, sub))
    assert got is not None
    assert all(0.0 <= got.uv(v.uv0)[0] <= 1.0 for v in verts)


def _strip(us, vs) -> carbin.Section:
    """Quads side by side along x sharing their edges: corner k at u us[k] (v vs[0] below,
    vs[1] above)."""
    verts = []
    for k, u in enumerate(us):
        verts += [carbin.Vertex((0.1 * k, 0.4, -1.0), (u, vs[0]), (0, 0), (0, 0, 1))]
        verts += [carbin.Vertex((0.1 * k, 0.6, -1.0), (u, vs[1]), (0, 0), (0, 0, 1))]
    idx = []
    for k in range(len(us) - 1):
        a, b, c, d = 2 * k, 2 * k + 1, 2 * k + 2, 2 * k + 3
        idx += [a, c, b, b, c, d]
    sub = carbin.SubSection("textured_reflector", 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, idx)
    return carbin.Section("body", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def test_tiling_mesh_across_a_tile_edge():
    # A reflector mesh continuous across u = 1 (the KIA Forte Koup's bowls, issue #189): the
    # vertex on the edge is in triangles of tiles 0 and 1. Each triangle is laid from its own
    # tile, and the vertex has a UV in each (the tile is in the vertex tag).
    strip = _strip([0.8, 1.0, 1.2, 1.4], (1.1, 1.3))
    sub = strip.subsections[0]
    m = fm.resolve(strip, sub)
    assert m.kind == "tiling"
    got = dm.unwrap_tiles(strip, sub, m)
    assert got is not None and got.collapse is None
    raw = [[strip.lod_vertices[i].uv0 for i in t] for t in sub.triangles()]
    assert got.tile(raw[0]) == (0, 1) and got.tile(raw[2]) == (1, 1)
    assert [round(u, 6) for u, _ in got.triangle(raw[0])] == [0.8, 1.0, 0.8]
    assert [round(u, 6) for u, _ in got.triangle(raw[2])] == [0.0, 0.2, 0.0]
    assert [round(v, 6) for _, v in got.triangle(raw[2])] == [0.1, 0.1, 0.3]


def test_coarse_lod_drifting_over_the_tile_edge():
    # Issue #229: the Dodge Viper SRT-10 03's LOD 5 reflector (u -0.06 to 0.15) while its finer
    # LODs lie inside tile 0 (u 0.01 to 0.84). Half its UV area past the edge was one flat
    # colour; laid triangle by triangle it samples the reflector as the finer LODs do.
    def part(fine_us, coarse_us):
        fine, coarse = _strip(fine_us, (0.2, 0.4)), _strip(coarse_us, (-0.4, 0.4))
        n = len(fine.lod_vertices)
        sub5 = carbin.SubSection(
            "textured_reflector", 5, 4, (0.0, 1.0, 0.0, 1.0) * 2, [i + n for i in coarse.subsections[0].indices]
        )
        verts = fine.lod_vertices + coarse.lod_vertices
        return carbin.Section(
            "body", (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[fine.subsections[0], sub5]
        ), sub5

    section, sub5 = part([0.1, 0.3, 0.6], [-0.4, 0.4])
    assert fm.resolve(section, sub5).kind == "tiling"
    got = dm.unwrap_tiles(section, sub5, fm.resolve(section, sub5))
    assert got is not None and got.kind == "textured" and got.drifted
    # Without finer LODs, or with finer LODs that tile themselves, it stays one colour.
    alone = _strip([-0.4, 0.4], (-0.4, 0.4))
    assert dm.unwrap_tiles(alone, alone.subsections[0], fm.resolve(alone, alone.subsections[0])) is None
    section, sub5 = part([0.8, 1.6, 2.4], [-0.4, 0.4])
    assert dm.unwrap_tiles(section, sub5, fm.resolve(section, sub5)) is None


def test_tiling_streak_laid_on_one_column():
    # The Shelby GT500 13's tail strips: a band repeated many times along u (here 5 tiles a
    # triangle), inside one tile along v. Laid on the column closest to each row's mean.
    strip = _strip([0.0, 5.0, 10.0, 15.0], (0.5, 0.75))
    sub = strip.subsections[0]
    m = fm.resolve(strip, sub)
    image = np.zeros((8, 8, 4), np.uint8)
    image[4:6, :, 0] = 100
    image[4:6, 3, 0] = 80
    image[4:6, 5, 0] = 98  # nearest the rows' mean (89.75)
    image[4:6, 6, 0] = 40
    got = dm.unwrap_tiles(strip, sub, m, image)
    assert got is not None and got.collapse == (0, 5.5 / 8), got.collapse
    raw = [[strip.lod_vertices[i].uv0 for i in t] for t in sub.triangles()]
    assert all(u == 5.5 / 8 for u, _ in got.triangle(raw[1]))
    assert sorted({round(v, 6) for _, v in got.triangle(raw[1])}) == [0.5, 0.75]
    # Without the atlas: the middle column.
    assert dm.unwrap_tiles(strip, sub, m).collapse == (0, 0.5)


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"{name}: ok")


def test_plain_glass_over_a_lamp_stays_window_glass():
    # Plain glass over a lamp (the Audi R8 race car's lexanLHL, the Mustang GT 05's head lamp
    # glass) is drawn as DSF window glass: Forza's one layer keeps 26% of the lamp head-on, DSF's
    # window glass about half; left out (#160, #247) the lamp showed whole (#379).
    lamp = _quad("headlightL", "head_light", -1.9, 0.5, 1.0, normal=(0.0, 0.0, -1.0))
    t = dm.Texturing(Path())
    t.lamp_tris = [(1, [lamp.lod_vertices[i].position for i in tri], None) for tri in lamp.subsections[0].triangles()]
    cover = _grid("lexanLHL", "glass", lambda x, y: -2.0, 0.0, 0.5, n=10)
    assert t._over_lamp(cover, cover.subsections[0]) > 0.1
    assert not hasattr(t, "clear_cover")


def test_front_lamp_view_leaves_out_dropped_lenses():
    # Issue #250: a lens left out (dropped_lenses) isn't drawn. Kept in the front view it hid the
    # lamp behind it, so the Venom GT's head lamps got no day glow under their left-out lexan.
    lens = _quad("lexanLHL", "detail_glass_clear", -2.0, 0.0, 0.5, normal=(0.0, 0.0, -1.0))
    lamp = _quad("headlightL", "head_light", -1.9, 0.5, 1.0, normal=(0.0, 0.0, -1.0))
    t = dm.Texturing(Path())
    t.atlases = _ClearAtlases()
    for sec in (lens, lamp):
        sub = sec.subsections[0]
        for tri in sub.triangles():
            pts = [sec.lod_vertices[i].position for i in tri]
            uvs = [sec.lod_vertices[i].uv0 for i in tri]
            t.lamp_tris.append((1, pts, uvs, (sec.name, sub.material)))
    _, u = t._lamp_view(front=True)
    assert (u < SIZE // 2).all()  # the lens's texels: it is in front
    t.dropped_lenses = {("lexanLHL", "detail_glass_clear")}
    _, u = t._lamp_view(front=True)
    assert (u >= SIZE // 2).all()  # the lamp's


def _quad_uv(u0, u1, w):
    return [[(u0 / w, 0.0), (u1 / w, 0.0), (u0 / w, 1.0)], [(u1 / w, 0.0), (u1 / w, 1.0), (u0 / w, 1.0)]]


def test_rear_lamp_glow_ranks_each_planned_lamp_onto_vanilla():
    # #290, #391: two planned tail lamps seen from behind, the left one's texels brighter than the
    # right's (the Panoz Esperante's sides differed): each is ranked on its own onto vanilla's
    # visible tail alpha, so both glow alike; a texel no planned tail lamp holds (the white reverse
    # lamp) keeps its alpha (NaN).
    h, w = 8, 64
    atlas = np.zeros((h, w, 4))
    atlas[:, :20] = (220, 10, 10, 250)  # left lamp's texels
    atlas[:, 20:40] = (120, 5, 5, 90)  # right lamp's: dimmer
    atlas[:, 40:50] = (230, 230, 230, 200)  # a white reverse lamp
    atlas[:, 0:40:2, 3] *= 0.6  # some detail
    gy, gx = np.mgrid[0:12, 0:30]
    grid = np.ones((12, 30), bool)
    # columns 13 to 15 see the white reverse lamp between the two red ones
    cols = np.where(gx < 13, gx, np.where(gx > 15, 20 + gx - 16, 40 + gx % 10))[grid]
    rows = (gy % h)[grid]
    tris = _quad_uv(0, 20, w) + _quad_uv(20, 40, w)
    view = ((rows, cols), grid, (0.0, 0.0), 0.005)
    out = lenses.rear_lamp_glow(atlas.astype(np.uint8), view, tris, [0, 0, 1, 1])
    left, right = out[:, :13], out[:, 20:34]
    assert np.isfinite(left).all() and np.isfinite(right).all()
    assert abs(np.percentile(left, 95) - np.percentile(right, 95)) < 25, (
        np.percentile(left, 95),
        np.percentile(right, 95),
    )
    assert np.isnan(out[:, 40:]).all()


def test_rear_lamp_glow_takes_grey_tail_texels():
    # #391: an FM4 tail lamp's texels are often grey (BrakeColor lights them red), and a clear lens
    # over a tail bakes grey too (the Aston Martin V12 Vantage): the planned lamp's texels are ranked
    # whatever their colour, in the order given (FM4: the texel's red).
    h, w = 8, 64
    atlas = np.zeros((h, w, 4))
    atlas[:, :40, :3] = (100 + 3 * np.arange(40))[:, None]  # grey texels, brighter to the right
    atlas[:, :40, 3] = 6  # at FM4's unread alpha
    gy, gx = np.mgrid[0:12, 0:30]
    grid = np.ones((12, 30), bool)
    view = ((gy[grid] % h, gx[grid]), grid, (0.0, 0.0), 0.005)
    order = atlas[..., 0] / 255
    out = lenses.rear_lamp_glow(atlas.astype(np.uint8), view, _quad_uv(0, 40, w), [3, 3], order=order)
    assert out is not None and np.isfinite(out[:, :30]).all()
    assert np.percentile(out[:, :30], 95) > 100  # on vanilla's tail alpha, not at FM4's alpha 6
    assert out[:, 25:30].mean() > out[:, 5:10].mean()  # brighter texels first


def test_fm4_order_puts_no_glow_techniques_last():
    # FM4 glows a tail by its texels' colour (BrakeColor x L) and never its reflectors: within a
    # planned lamp the grey reflector texels rank below the red tail graphics, however bright.
    h, w = 4, 64
    colour = np.zeros((h, w, 4), np.uint8)
    colour[:, :20, :3] = (93, 12, 16)  # tail_light graphics
    colour[:, 20:40, :3] = (139, 139, 139)  # reflector the plan lights with the tail
    uvs = np.array(_quad_uv(0, 20, w) + _quad_uv(20, 40, w), float)
    order = dm.fm4_order(colour, uvs, np.array([False, False, True, True]))
    assert order[:, 5].min() > 0 and (order[:, 25] == 0).all()
    assert dm.fm4_car(Path("out/cache/fm4/AST_V12Vantage_10")) and not dm.fm4_car(Path("out/cache/fh1/X"))
