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


def _strip(name: str, material: str, x: float, z: float, n: int) -> carbin.Section:
    """n triangles side by side, 0.1 m wide each, at height 0.4 to 0.6."""
    pts = [(x - 0.05 * n + 0.1 * (i // 2), 0.4 + 0.2 * (i % 2), z) for i in range(2 * n + 2)]
    verts = [carbin.Vertex(p, (0, 0), (0, 0), (0.0, 0.0, 1.0)) for p in pts]
    indices = [i + j for i in range(0, 2 * n, 2) for j in (0, 1, 2)]
    sub = carbin.SubSection(material, 1, 4, (0.0, 1.0, 0.0, 1.0) * 2, indices)
    return carbin.Section(name, (0, 0, 0), (0, 0, 0), (0, 0, 0), lod_vertices=verts, subsections=[sub])


def test_red_amber_lens_lights_with_the_brakes():
    # FM4 LOT_Carlton_89: the tail lamps are one detail_glass_amber lens on red texels, the
    # tail_light bulb behind it. By technique alone the lens was an indicator (no brake light).
    car = [
        _quad("glassLTL", "detail_glass_amber", 0.6, -2.05),
        _quad("glassRTL", "detail_glass_amber", -0.6, -2.05),
        _quad("tailL", "tail_light", 0.6, -2.0),
        _quad("tailR", "tail_light", -0.6, -2.0),
    ]
    red = {"glassLTL": RED_TEXEL, "glassRTL": RED_TEXEL}
    # no other rear indicator (MIT_GTO_97): a red lamp that brakes and indicates
    got = _functions(car, red)
    assert got["glassLTL"] == got["glassRTL"] == lights.BRAKE_IND, got
    # with amber indicators of their own at the rear: a tail lamp
    ind = [_quad("indL", "indicator_left", 0.8, -2.0), _quad("indR", "indicator_right", -0.8, -2.0)]
    got = _functions(car + ind, red)
    assert got["glassLTL"] == got["glassRTL"] == lights.TAIL, got
    assert _functions(car, {})["glassLTL"] == lights.INDICATOR  # no texels: the technique decides
    assert _functions(car, {"glassLTL": AMBER_TEXEL})["glassLTL"] == lights.INDICATOR
    # all white: an amber lens tinted by its technique
    assert _functions(car, {"glassLTL": CLEAR_TEXEL})["glassLTL"] == lights.INDICATOR


def test_mostly_red_lens_is_red():
    # red with a white rim triangle: the whole lens is red (no indicator speckles in it);
    # an amber cell keeps indicating
    lens = _strip("glassLTL", "detail_glass_amber", 0.6, -2.05, 5)
    sub = lens.subsections[0]
    colours = [RED_TEXEL, CLEAR_TEXEL, RED_TEXEL, RED_TEXEL, AMBER_TEXEL]
    plan = lights.LampPlan.build([lens], _Place(), 0.0, texel=lambda sec, sub, tri: colours[tri[0] // 2])
    got = [plan.tri_lamp[(id(sub), k)][0] for k in range(5)]
    assert got == [lights.TAIL] * 4 + [lights.INDICATOR], got
    # not mostly red: the clear part stays the technique's amber
    colours = [RED_TEXEL, CLEAR_TEXEL, CLEAR_TEXEL, RED_TEXEL, AMBER_TEXEL]
    plan = lights.LampPlan.build([lens], _Place(), 0.0, texel=lambda sec, sub, tri: colours[tri[0] // 2])
    got = [plan.tri_lamp[(id(sub), k)][0] for k in range(5)]
    assert got == [lights.TAIL, lights.INDICATOR, lights.INDICATOR, lights.TAIL, lights.INDICATOR], got


def test_red_texels_rear_only():
    # front amber lenses keep their technique whatever their texels (indicators)
    car = [_quad("glassLHL", "detail_glass_amber", 0.6, 2.0), _quad("glassRHL", "detail_glass_amber", -0.6, 2.0)]
    got = _functions(car + TAILS, {"glassLHL": RED_TEXEL, "glassRHL": RED_TEXEL})
    assert got["glassLHL"] == got["glassRHL"] == lights.INDICATOR, got


def test_clear_lens_on_red_texels_is_tail():
    # a rear clear lens drawn on red texels lights with the brakes, not with the reverse lamp
    car = [
        _quad("glassLTL", "detail_glass_clear", 0.6, -2.05),
        _quad("revL", "reverse_light", 0.75, -2.0),
        _quad("revR", "reverse_light", -0.75, -2.0),
    ]
    assert _functions(car, {})["glassLTL"] == lights.REVERSE
    assert _functions(car, {"glassLTL": RED_TEXEL})["glassLTL"] == lights.TAIL
    # ...but right in front of the reverse bulb it stays the reverse lamp (PEU_207RC_07)
    car[1] = _quad("revL", "reverse_light", 0.6, -2.0)
    assert _functions(car, {"glassLTL": RED_TEXEL})["glassLTL"] == lights.REVERSE


def test_red_lens_over_indicator_blinks():
    # FOR_GT_05: red lenses over drlinl/drlinr lamps. Stock lamps are opaque, so the bulb
    # behind never shows: the lens itself brakes and indicates.
    car = [
        _quad("glassLTL", "detail_glass_red", 0.6, -2.05),
        _quad("glassRTL", "detail_glass_red", -0.6, -2.05),
        _quad("indL", "drlinlred", 0.6, -2.0),
        _quad("indR", "drlinrred", -0.6, -2.0),
        _quad("tailL", "tail_light", 0.6, -1.8),
    ]
    got = _functions(car, {"glassLTL": RED_TEXEL, "glassRTL": RED_TEXEL})
    assert got["glassLTL"] == got["glassRTL"] == lights.BRAKE_IND, got
    plan = lights.LampPlan.build(car, _Place(), 0.0)
    flags = {l.side: l.flags >> 24 for l in plan.lamps.values() if l.function == lights.BRAKE_IND}
    assert flags == {"L": 0x19, "R": 0x1A}, flags


def test_red_lens_keeps_clear_lenses_on_their_lamp():
    # JAG_XFR_10: the tail lamp's red-texel lens joins the tail lamp; a clear lens over the
    # tail lamp's end still lights with it, not with the reverse lamp now nearer its centre
    tail = _quad("tailL", "tail_light", 0.75, -2.0)
    rev = _quad("revL", "reverse_light", 0.35, -2.0)
    red = _strip("glassRed", "detail_glass_clear", 1.2, -2.05, 6)  # x 0.9 to 1.5: pulls the tail centre out
    clear = _quad("glassL", "detail_glass_clear", 0.62, -2.05)
    got = _functions([tail, rev, red, clear], {"glassRed": RED_TEXEL})
    assert got["glassRed"] == lights.TAIL and got["glassL"] == lights.TAIL, got


def test_transparent_red_is_clear():
    assert lights.texel_class((123, 0, 0, 0)) == "white"  # VAU_Agila_11's clear lens


class _Upvh:  # what LampPlan.assign reads of a parsed UPVH: n light entries with ids 0..n-1
    def __init__(self, n: int):
        import struct

        self.offsets, self.counts = {"lights": 0}, {"lights": n}
        self.data = b"".join(b"\0" * 40 + struct.pack("<I", i) + b"\0" * 20 for i in range(n))


def _assigned(sections, entries: int) -> dict:
    from dsfforza.formats import upvh as uv

    parse, uv.parse = uv.parse, lambda data: _Upvh(entries)
    try:
        plan = lights.LampPlan.build(sections, _Place(), 0.0).assign(b"")
    finally:
        uv.parse = parse
    return {(l.function, l.side, l.front): l.flags >> 24 for l in plan.lamps.values()}


def test_head_lamps_light_by_day():
    # Stock cars light a front lamp with DaytimeRunning (0x03 position lamps, or 0x1D head lamps
    # on 31 of 150); ported head lamps were 0x01 only and stayed dark by day (player report)
    heads = [_quad("headlightL", "head_light", 0.6, 2.0), _quad("headlightR", "head_light", -0.6, 2.0)]
    got = _assigned(heads + TAILS, 8)
    assert got[(lights.HEAD, "L", True)] == got[(lights.HEAD, "R", True)] == 0x1D, got
    # with a position lamp of its own on that side, the head lamp stays 0x01 (stock 229)
    drl = [_quad("drlL", "drlwhite", 0.8, 2.0)]
    got = _assigned(heads + drl + TAILS, 8)
    assert got[(lights.HEAD, "L", True)] == 0x01 and got[(lights.HEAD, "R", True)] == 0x1D, got
    assert got[(lights.POSITION, "L", True)] == 0x03, got


def test_pop_up_lamps_keep_the_lowered_state():
    # Issue #88: both states were drawn at once. RX-7 FC: up/down pairs; NSX: "Up"; Starion:
    # glass pairs too; Stratos: headlightRup beside the lowered headlightR. A lone "...up" stays.
    from dsfforza.convert import forza2dsf

    names = [
        "body",
        "headlightLup",
        "headlightLdown",
        "headlightRUp",
        "headlightRdown",
        "glassLHLup",
        "glassLHLdown",
        "headlightL2up",
        "headlightL2",
        "cagerace",
        "lonelyup",
    ]
    sections = [carbin.Section(n, (0, 0, 0), (0, 0, 0), (0, 0, 0)) for n in names]
    kept = [s.name for s in forza2dsf.stock_sections(sections)]
    assert kept == ["body", "headlightLdown", "headlightRdown", "glassLHLdown", "headlightL2", "lonelyup"], kept


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
