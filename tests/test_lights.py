"""lights.LampPlan head lamps for cars without a head-lamp technique, and lens
colours from their texels (synthetic sections).

uv run python tests/test_lights.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.convert import lights
from dsfforza.formats import carbin


class _Place:  # identity placement: Forza space is DSF space here
    def point(self, p):
        return tuple(p)

    def normal(self, n):
        return tuple(n)


def _quad(name: str, material: str, x: float, z: float) -> carbin.Section:
    pts = [(x - 0.1, 0.4, z), (x + 0.1, 0.4, z), (x - 0.1, 0.6, z), (x + 0.1, 0.6, z)]
    verts = [carbin.Vertex(p, (0, 0), (0, 0), (0.0, 0.0, 1.0)) for p in pts]
    sub = carbin.SubSection(material, 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, [0, 1, 2, 2, 1, 3])
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def _heads(sections) -> list:
    plan = lights.LampPlan.build(sections, _Place(), 0.0)
    return sorted((l.side, l.front) for l in plan.lamps.values() if l.function == lights.HEAD)


TAILS = [_quad("taillightL", "tail_light", 0.6, -2.0), _quad("taillightR", "tail_light", -0.6, -2.0)]


def test_head_technique():
    cars = [_quad("headlightL", "oldhead", 0.6, 2.0), _quad("headlightR", "oldhead", -0.6, 2.0)]
    assert _heads(cars + TAILS) == [("L", True), ("R", True)]


def test_clear_lens_is_head_lamp():
    # CHE_CorvetteGS_10: reflector under a clear lens, no head-lamp technique
    cars = [
        _quad("glassLHL", "detail_glass_clear", 0.6, 2.0),
        _quad("glassRHL", "detail_glass_clear", -0.6, 2.0),
        _quad("headlightL", "textured_reflector", 0.6, 1.95),
        _quad("headlightR", "textured_reflector", -0.6, 1.95),
    ]
    assert _heads(cars + TAILS) == [("L", True), ("R", True)]


def test_reflectors_then_covers():
    # DOD_RamSRT10_06-like: reflectors, amber lens only; MIT_HKS_06: cover glass only
    ram = [_quad("headlightL", "textured_reflector", 0.6, 2.0), _quad("headlightR", "textured_reflector", -0.6, 2.0)]
    assert _heads(ram + TAILS) == [("L", True), ("R", True)]
    hks = [_quad("headlightL", "glass", 0.6, 2.0), _quad("headlightR", "glass", -0.6, 2.0)]
    assert _heads(hks + TAILS) == [("L", True), ("R", True)]


def test_decal_headlights_stay_dark():
    # NASCAR (CHE_2_MonteCarloSS_11): the headlights are badge decals
    cars = [_quad("headlightL", "badge", 0.6, 2.0), _quad("headlightR", "badge", -0.6, 2.0)]
    assert _heads(cars + TAILS) == []


RED_TEXEL, AMBER_TEXEL, CLEAR_TEXEL = (140, 40, 40, 250), (200, 120, 30, 250), (200, 200, 195, 255)


def test_texel_class():
    assert lights.texel_class(RED_TEXEL) == "red"
    assert lights.texel_class((31, 2, 2, 255)) == "red"  # a near-black red lens (S2000)
    assert lights.texel_class((160, 86, 52, 250)) == "amber"  # the Carlton's indicator band
    assert lights.texel_class(AMBER_TEXEL) == "amber"
    assert lights.texel_class(CLEAR_TEXEL) == "white"
    assert lights.texel_class((40, 60, 200, 255)) == "white"  # blue: not a lamp colour
    assert lights.texel_class(None) is None


def _functions(sections, texels: dict) -> dict:
    """{(section name): function of its lamp} with texel colours per section name."""
    by_sub = {id(sub): texels.get(sec.name) for sec in sections for sub in sec.subsections}
    plan = lights.LampPlan.build(sections, _Place(), 0.0, texel=lambda sec, sub, tri: by_sub[id(sub)])
    out = {}
    for sec in sections:
        for sub in sec.subsections:
            key = plan.tri_lamp.get((id(sub), 0))
            out[sec.name] = key[0] if key else None
    return out


def test_red_amber_lens_is_tail_lamp():
    # FM4 LOT_Carlton_89: the tail lamps are one detail_glass_amber lens on red texels, the
    # tail_light bulb behind it. By technique alone the lens was an indicator (no brake light).
    car = [
        _quad("glassLTL", "detail_glass_amber", 0.6, -2.05),
        _quad("glassRTL", "detail_glass_amber", -0.6, -2.05),
        _quad("tailL", "tail_light", 0.6, -2.0),
        _quad("tailR", "tail_light", -0.6, -2.0),
    ]
    red = {"glassLTL": RED_TEXEL, "glassRTL": RED_TEXEL}
    got = _functions(car, red)
    assert got["glassLTL"] == got["glassRTL"] == lights.TAIL, got
    assert _functions(car, {})["glassLTL"] == lights.INDICATOR  # no texels: the technique decides
    assert _functions(car, {"glassLTL": AMBER_TEXEL})["glassLTL"] == lights.INDICATOR
    # all white: an amber lens tinted by its technique
    assert _functions(car, {"glassLTL": CLEAR_TEXEL})["glassLTL"] == lights.INDICATOR
    # red with a white rim triangle: the whole lens is red (no indicator speckles in it)
    by_tri = {"glassLTL": lambda tri: RED_TEXEL if tri[0] == 0 else CLEAR_TEXEL}
    plan = lights.LampPlan.build(
        car, _Place(), 0.0, texel=lambda sec, sub, tri: by_tri[sec.name](tri) if sec.name in by_tri else None
    )
    sub = car[0].subsections[0]
    assert [plan.tri_lamp[(id(sub), k)][0] for k in (0, 1)] == [lights.TAIL, lights.TAIL]


def test_red_texels_rear_only():
    # front amber lenses keep their technique whatever their texels (indicators)
    car = [_quad("glassLHL", "detail_glass_amber", 0.6, 2.0), _quad("glassRHL", "detail_glass_amber", -0.6, 2.0)]
    got = _functions(car + TAILS, {"glassLHL": RED_TEXEL, "glassRHL": RED_TEXEL})
    assert got["glassLHL"] == got["glassRHL"] == lights.INDICATOR, got


def test_clear_lens_on_red_texels_is_tail():
    # a rear clear lens drawn on red texels lights with the brakes, not with the reverse lamp
    car = [
        _quad("glassLTL", "detail_glass_clear", 0.6, -2.05),
        _quad("revL", "reverse_light", 0.6, -2.0),
        _quad("revR", "reverse_light", -0.6, -2.0),
    ]
    assert _functions(car, {})["glassLTL"] == lights.REVERSE
    assert _functions(car, {"glassLTL": RED_TEXEL})["glassLTL"] == lights.TAIL


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
