"""lenses.bake (Forza lamp lenses baked over what lies behind them) and the red lamp
curve's knot spacing, on synthetic sections and atlases.

uv run python tests/test_lenses.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

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
    a = LENS[3] / 255
    rgb = [round(a * l + (1 - a) * b) for l, b in zip(LENS[:3], LAMP[:3])]
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    # The glow stays the lens's: the same lamp score as the lens texel.
    assert abs(dm.lamp_score(*got) - dm.lamp_score(*LENS)) < 0.01, (got, LENS)
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
    assert centre > 150 and rim < centre / 3, (centre, rim)


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


def test_daytime_head_lamps_glow_at_a_fifth():
    # A clear lens over a head lamp bowl whose left half lights by day (0x1D) and right half
    # doesn't (an indicator behind the same lens, the Lancer Evo VI's): only the left half's
    # glow is scaled (DAYTIME_GLOW).
    lens = _grid("glassLHL", "detail_glass_clear", lambda x, y: -2.0, 0.0, 0.5)
    bowl = _grid("headlightL", "head_light", lambda x, y: -2.0 + 0.1 - 4.5 * (x * x + (y - 0.5) ** 2), 0.5, 1.0)
    t = dm.Texturing(Path(), size=(32, 32))
    t.atlases, t.families = _ClearAtlases(), (fm.LIGHTS,)
    t.lamp_raw = lenses.new_glow(SIZE, SIZE)
    with tempfile.TemporaryDirectory() as car_dir:
        ids = {id(s) for s in lens.subsections}
        lenses.bake([lens, bowl], {1}, _ClearAtlases(), ids, Path(car_dir), glow=t.lamp_raw, owners=t.glow_owners)
    sub = lens.subsections[0]
    assert {id(s) for _, s in t.glow_owners.values()} == {id(sub)}
    t.resolved = {(id(sec), id(s)): fm.resolve(sec, s) for sec in (lens, bowl) for s in sec.subsections}
    assert t.daytime_mask([lens, bowl], lambda s, k: False) == (None, set())
    # Triangle k of the 8 x 8 grid is in column (k // 2) % 8: columns 0-3 are u 0 to 0.25.
    day, cells = t.daytime_mask([lens, bowl], lambda s, k: s is sub and (k // 2) % 8 < 4)
    assert not cells and day[:, :6].all() and not day[:, 12:].any(), day[0]

    t.regions = {fm.LIGHTS: [None]}
    t._lamp_view = lambda front, grid: "view"
    flat = np.where(np.isfinite(t.lamp_raw[..., 0]), 100.0, np.nan)
    real, lenses.day_glow = lenses.day_glow, lambda glow, view: flat.copy()
    try:
        t.daytime_texels = day
        out = t._glow()
    finally:
        lenses.day_glow = real
    assert (out[day] == 100.0 * dm.DAYTIME_GLOW).all()
    assert (out[:, 12:16] == 100.0).all()
    # A day-lit lamp surface without a glow (a reflector, the bowl here) keeps its texels
    # but a fifth of their converted alpha; a tiling one, its swatch cell.
    day, _ = t.daytime_mask([lens, bowl], lambda s, k: s is bowl.subsections[0])
    assert day[:, 16:].all() and not day[:, 12:16].any()


def test_render_scales_daytime_alpha():
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
    t.daytime_texels = np.zeros((SIZE, SIZE), bool)
    t.daytime_texels[:, 8:12] = True
    out = np.frombuffer(bytes(t.render()), np.uint8).reshape(32, 32, 4)
    plain = t._convert_texel(fm.LIGHTS, 180, 180, 180, 100)[3]
    assert out[dm.PAD + 2, dm.PAD + 1, 3] == round(plain * dm.DAYTIME_GLOW)  # source x 9: day-lit
    assert out[dm.PAD + 2, dm.PAD + 6, 3] == plain  # source x 14: as converted


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
    # Lenses: the texel alpha, capped by the car's GlassMaxOpacity only. Lamp glass: Forza's
    # opacity starts at 1, so it is GlassMaxOpacity. Covers: max(texel alpha, GlassColor.w).
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
    # The red lens filter: red remapped by DarkValues / LightValues (detail_glass_red.fxobj).
    red = {"detail_glass_red": {"DarkValues": 0.4, "LightValues": 1.5}}
    f = lenses.lens_filter("detail_glass_red", (155, 0, 2), red)
    assert abs(f[0] - (0.4 + 1.1 * (155 / 255) ** 2) ** 0.5) < 1e-9 and f[1] == 0.0
    assert not lenses.emits("lights_glass") and lenses.emits("lights_gls_reverse_light")


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
    # A cover (lights_glass) has no glow of its own: the lamp's behind it.
    cover = _quad("glassLTL", "lights_glass", 2.0, 0.0, 0.5)
    lamp = _quad("taillightL", "textured_reflector", 1.9, 0.5, 1.0)
    atlases, _ = _bake([cover, lamp], [cover])
    got = atlases.texel(SIZE // 4, SIZE // 2)
    want = dm.lamp_score(*LAMP)
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


def test_flat_rear_lens_tints_the_lamp_behind():
    # A rear lens of one flat colour however fine its texels (the BMW X6 M's red tail glass, 40,000
    # texels of (183, 0, 0, 118)) is a swatch lens: left out, its tint over the lamp behind.
    lens = _quad("glassLTL", "detail_glass_red", 2.0, 0.0, 0.5)
    lamp = _quad("taillightL", "textured_reflector", 1.9, 0.5, 1.0)
    dropped = set()
    atlases, report = _bake([lens, lamp], [lens], dropped=dropped)
    assert dropped == {("glassLTL", "detail_glass_red")}
    rgb = [round(b * l / 255) for l, b in zip(LENS[:3], LAMP[:3])]
    got = atlases.texel(SIZE * 3 // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    assert got[3] == LAMP[3]
    # A flat front lens is baked as before.
    lens = _quad("glassLHL", "detail_glass_red", -2.0, 0.0, 0.5, normal=(0.0, 0.0, -1.0))
    lamp = _quad("headlightL", "textured_reflector", -1.9, 0.5, 1.0, normal=(0.0, 0.0, -1.0))
    dropped = set()
    atlases, _ = _bake([lens, lamp], [lens], dropped=dropped)
    assert not dropped
    assert atlases.texel(SIZE // 4, SIZE // 2) != LENS


def test_laid_back_front_lens_sees_the_lamp_along_the_axis():
    # A front lens facing up (the Panoz Esperante's head lamps): along its normal nothing, along
    # the car's axis the lamp behind it.
    lens = _quad("glassLHL", "detail_glass_clear", -2.0, 0.0, 0.5, normal=(0.0, 1.0, 0.0))
    lamp = _quad("headlightL", "textured_reflector", -1.9, 0.5, 1.0, normal=(0.0, 0.0, -1.0))
    atlases, report = _bake([lens, lamp], [lens])
    a = LENS[3] / 255
    rgb = [round(a * l + (1 - a) * b) for l, b in zip(LENS[:3], LAMP[:3])]
    got = atlases.texel(SIZE // 4, SIZE // 2)
    assert all(abs(g - w) <= 1 for g, w in zip(got[:3], rgb)), (got, rgb)
    assert report["glassLHL/detail_glass_clear"]["behind"] == 1.0


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
    import dataclasses

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
    # A triangle across a tile edge can't be laid on the atlas.
    edge = _quad("body", "textured_reflector", -1.0, 2.9, 3.4)
    assert dm.unwrap_tiles(edge, edge.subsections[0], fm.resolve(edge, edge.subsections[0])) is None


if __name__ == "__main__":
    for name, test in list(globals().items()):
        if name.startswith("test_"):
            test()
            print(f"{name}: ok")
