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


def test_yellow_front_lens_is_head_lamp_cover():
    # PAN_51_Esperante_05: yellow lexan (detail_glass_amber, hue about 50) over the head lamps
    # lights with them; an orange-amber front lens (the Mustang GT's bumper lamps) stays an
    # indicator even with a head_light bulb behind it
    yellow = (134, 114, 1, 162)
    assert lights.yellow_lens(yellow) and not lights.yellow_lens(AMBER_TEXEL)
    assert not lights.yellow_lens((145, 73, 26, 164)) and not lights.yellow_lens(CLEAR_TEXEL)
    car = [
        _quad("lexanLHL", "detail_glass_amber", 0.6, 2.05),
        _quad("headL", "head_light", 0.6, 2.0),
        _quad("lexanRHL", "detail_glass_amber", -0.6, 2.05),
        _quad("headR", "head_light", -0.6, 2.0),
    ] + TAILS
    got = _functions(car, {"lexanLHL": yellow, "lexanRHL": yellow})
    assert got["lexanLHL"] == got["lexanRHL"] == lights.HEAD, got
    got = _functions(car, {"lexanLHL": (145, 73, 26, 164), "lexanRHL": (145, 73, 26, 164)})
    assert got["lexanLHL"] == got["lexanRHL"] == lights.INDICATOR, got


def test_clear_lens_on_red_texels_is_tail():
    # a rear clear lens drawn on red texels lights with the brakes, not with the reverse lamp
    car = [
        _quad("glassLTL", "detail_glass_clear", 0.6, -2.05),
        _quad("revL", "reverse_light", 0.68, -2.0),  # behind part of the lens
        _quad("revR", "reverse_light", -0.68, -2.0),
    ]
    assert _functions(car, {})["glassLTL"] == lights.REVERSE
    assert _functions(car, {"glassLTL": RED_TEXEL})["glassLTL"] == lights.TAIL
    # ...but right in front of the reverse bulb it stays the reverse lamp (PEU_207RC_07)
    car[1] = _quad("revL", "reverse_light", 0.6, -2.0)
    assert _functions(car, {"glassLTL": RED_TEXEL})["glassLTL"] == lights.REVERSE


def test_red_lens_over_indicator_blinks():
    # FOR_GT_05: red lenses over drlinl/drlinr lamps. Vanilla lamps are opaque, so the bulb
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


class _Upvh:  # what LampPlan.assign reads of a parsed UPVH: light entries with ids 0..n-1
    def __init__(self, n, places=None):
        import struct

        places = places or [(0.0, 1.0)] * n  # (x, z) of each entry as shipped (default: front centre)
        self.offsets, self.counts = {"lights": 0}, {"lights": len(places)}
        self.data = b"".join(
            struct.pack("<3f", x, 0.5, z) + b"\0" * 28 + struct.pack("<I", i) + b"\0" * 20
            for i, (x, z) in enumerate(places)
        )


def _plan(sections, entries, places=None):
    from dsfforza.formats import upvh as uv

    parse, uv.parse = uv.parse, lambda data: _Upvh(entries, places)
    try:
        return lights.LampPlan.build(sections, _Place(), 0.0).assign(b"")
    finally:
        uv.parse = parse


def _assigned(sections, entries: int) -> dict:
    plan = _plan(sections, entries)
    return {(l.function, l.side, l.front): l.flags >> 24 for l in plan.lamps.values()}


def test_lamps_get_entries_at_their_own_end_and_side():
    # Issue #132: the Citroen DS3's and Mazda 2's head lamps broke with the tail lamps. A light
    # breaks with the panel its entry hangs under; entries were handed out in table order.
    heads = [_quad("headlightL", "head_light", 0.6, 2.0), _quad("headlightR", "head_light", -0.6, 2.0)]
    places = [(0.6, -2.0), (-0.6, -2.0), (-0.6, 2.0), (0.6, 2.0)]  # rear L, rear R, front R, front L
    got = {(l.function, l.side): l.light_id for l in _plan(heads + TAILS, 4, places).lamps.values()}
    assert got == {(lights.HEAD, "L"): 3, (lights.HEAD, "R"): 2, (lights.TAIL, "L"): 0, (lights.TAIL, "R"): 1}, got
    # Only rear entries: the head lamps still light, on them, rather than stay dark.
    plan = _plan(heads + TAILS, 4, [(0.6, -2.0), (-0.6, -2.0), (0.6, -2.1), (-0.6, -2.1)])
    assert all(l.light_id is not None for l in plan.lamps.values())
    # A rear indicator without an entry of its own doesn't share the front one's.
    inds = [_quad("indFL", "indicator_left", 0.7, 2.0), _quad("indRL", "indicator_left", 0.7, -2.0)]
    plan = _plan(heads + TAILS + inds, 5, [(0.6, 2.0), (-0.6, 2.0), (0.6, -2.0), (-0.6, -2.0), (0.7, 2.0)])
    front = next(l for l in plan.lamps.values() if l.function == lights.INDICATOR and l.front)
    rear = next(l for l in plan.lamps.values() if l.function == lights.INDICATOR and not l.front)
    assert front.light_id == 4 and rear.light_id != 4, (front.light_id, rear.light_id)


def test_head_lamps_light_by_day():
    # Vanilla cars light a front lamp with DaytimeRunning (0x03 position lamps, or 0x1D head lamps
    # on 31 of 150); ported head lamps were 0x01 only and stayed dark by day (player report)
    heads = [_quad("headlightL", "head_light", 0.6, 2.0), _quad("headlightR", "head_light", -0.6, 2.0)]
    got = _assigned(heads + TAILS, 8)
    assert got[(lights.HEAD, "L", True)] == got[(lights.HEAD, "R", True)] == 0x1D, got
    # with a position lamp of its own on that side, the head lamp stays 0x01 (vanilla 229)
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
    kept = [s.name for s in forza2dsf.vanilla_sections(sections)]
    assert kept == ["body", "headlightLdown", "headlightRdown", "glassLHLdown", "headlightL2", "lonelyup"], kept


def test_head_lamp_unit_without_a_lamp_technique():
    # FOR_MustangGT_05 (issue #158): the round bowls are a textured_reflector in `body` under
    # the headlightL/R plain glass, and the car's only head lamp technique is elsewhere (the
    # head_light under its bumper lenses). The bowls light as the head lamp; a reflector
    # outside the cover, and one under a cover with its own head lamp, stay unlit.
    bowls = [_quad("body", "textured_reflector", 0.6, 1.9), _quad("body", "textured_reflector", -0.6, 1.9)]
    covers = [_quad("headlightL", "glass", 0.6, 2.0), _quad("headlightR", "glass", -0.6, 2.0)]
    bumper = [_quad("bumperFa", "head_light", 0.25, 2.1), _quad("bumperFa", "head_light", -0.25, 2.1)]
    other = _quad("bumperFa", "reflector", 0.0, 2.1)
    plan = lights.LampPlan.build(bowls + covers + bumper + [other] + TAILS, _Place(), 0.0)

    def lit(section):
        key = plan.tri_lamp.get((id(section.subsections[0]), 0))
        return None if key is None else (plan.lamps[key].function, plan.lamps[key].side)

    assert [lit(s) for s in bowls] == [(lights.HEAD, "L"), (lights.HEAD, "R")]
    assert lit(other) is None
    # With a head lamp technique under the cover, its reflectors are left as they were.
    own = [_quad("headlightL", "head_light", 0.6, 1.95), _quad("headlightR", "head_light", -0.6, 1.95)]
    plan = lights.LampPlan.build(bowls + covers + own + TAILS, _Place(), 0.0)
    assert [lit(s) for s in bowls] == [None, None]


def test_rear_lamps_short_of_rear_entries_stay_at_the_rear():
    # Issue #146: a host with fewer rear entries than the car has rear lamps. The right reverse
    # lamp shares the left one's entry rather than take a front entry (it would break with the
    # front); the rear fog lamp, never lit (no Lua sets FogLights), stays unlit.
    heads = [_quad("headlightL", "head_light", 0.6, 2.0), _quad("headlightR", "head_light", -0.6, 2.0)]
    revs = [_quad("revL", "reverse_light", 0.4, -2.0), _quad("revR", "reverse_light", -0.4, -2.0)]
    fog = [_quad("fogR", "fogred", 0.0, -2.1)]
    places = [(0.6, 2.0), (-0.6, 2.0), (0.6, 2.1), (-0.6, 2.1), (0.6, -2.0), (-0.6, -2.0), (0.4, -2.0)]
    plan = _plan(heads + TAILS + revs + fog, 7, places)
    rear = {e for e, (x, z) in enumerate(places) if z < 0}
    got = {(l.function, l.side): l.light_id for l in plan.lamps.values() if not l.front}
    assert got[(lights.REVERSE, "L")] == got[(lights.REVERSE, "R")] in rear, got
    assert got[(lights.FOG, "C")] is None, got
    assert all(i in rear for k, i in got.items() if k[0] != lights.FOG), got


def test_tailside_lamps_brake():
    # CHE_CorvetteZR1_09 (issue #103): its four round tail lamps are lindtailside/rindtailside,
    # which the cars that set them give a BrakeColor: tail, brake and indicator (0x19/0x1A).
    car = [_quad("taillightL", "lindtailside", 0.6, -2.0), _quad("taillightR", "rindtailside", -0.6, -2.0)]
    got = _assigned(car, 8)
    assert got[(lights.BRAKE_IND, "L", False)] == 0x19 and got[(lights.BRAKE_IND, "R", False)] == 0x1A, got


def test_clear_cover_lights_only_with_a_lamp_behind_it():
    # VW_GTIVR6_98 (issue #174): bumperFa's clear covers lie over reflectors with no lamp of
    # their own, 0.3 m from the head lamp. They used to light with the nearest lamp (the head
    # lamp); now only a lamp behind a cover lights it. The head lamp's own lens still does,
    # and so does a rear reflector cup of the tail lamp's section beside its bulb.
    head = [_quad("headlightL", "head_light", 0.6, 1.95), _quad("headlightR", "head_light", -0.6, 1.95)]
    lens = [_quad("glassLHL", "detail_glass_clear", 0.6, 2.0), _quad("glassRHL", "detail_glass_clear", -0.6, 2.0)]
    fog = [_quad("bumperFa", "detail_glass_clear", 0.35, 2.1), _quad("fogL", "textured_reflector", 0.35, 2.05)]
    cup = _quad("taillightL", "reflector", 0.85, -1.95)
    got = _functions(head + lens + fog + TAILS + [cup], {})
    assert got["glassLHL"] == got["glassRHL"] == lights.HEAD, got
    assert got["bumperFa"] is None, got
    assert got["taillightL"] == lights.TAIL, got


def _seen_functions(sections, seen: dict) -> dict:
    """{section name: [function of each triangle's lamp]} with the drawn texel class per section
    name (LampPlan.build `seen`)."""
    by_sub = {id(sub): seen.get(sec.name, "white") for sec in sections for sub in sec.subsections}
    plan = lights.LampPlan.build(sections, _Place(), 0.0, seen=lambda sec, sub, tri: by_sub[id(sub)])
    out = {}
    for sec in sections:
        for sub in sec.subsections:
            keys = [plan.tri_lamp.get((id(sub), k)) for k in range(len(list(sub.triangles())))]
            out[sec.name] = [key[0] if key else None for key in keys]
    return out


def test_red_drawn_reverse_and_indicator_brake():
    # Issue #103: Forza draws some reverse and indicator bulbs on red texels. Lit as reverse and
    # amber indicator lamps they lit white or amber when reversing or indicating and never braked.
    car = [
        _quad("revL", "reverse_light", 0.4, -2.0),
        _quad("revL2", "reverse_light", 0.3, -2.0),
        _quad("revR", "reverse_light", -0.4, -2.0),
        _quad("indL", "indicator_left", 0.8, -2.0),
        _quad("indR", "indicator_right", -0.8, -2.0),
    ] + TAILS
    got = _seen_functions(car, {"revL": "red", "indL": "red", "indR": "red"})
    assert got["revL"] == [lights.TAIL] * 2 and got["revL2"] == got["revR"] == [lights.REVERSE] * 2, got
    # a rear indicator side drawn red brakes and indicates (vanilla's US-style 0x19/0x1A)
    assert got["indL"] == got["indR"] == [lights.BRAKE_IND] * 2, got
    # all of a side's reverse lamp drawn red: it stays the reverse lamp (VW_GolfR_10's bulbs)
    got = _seen_functions(car, {"revR": "red"})
    assert got["revR"] == [lights.REVERSE] * 2, got
    # white and amber ones keep their technique
    got = _seen_functions(car, {"indL": "amber"})
    assert got["revL"] == [lights.REVERSE] * 2 and got["indL"] == [lights.INDICATOR] * 2, got
    # without the drawn texels nothing changes
    plan = lights.LampPlan.build(car, _Place(), 0.0)
    assert {l.function for l in plan.lamps.values()} == {lights.REVERSE, lights.INDICATOR, lights.TAIL}


def test_red_reflector_round_a_reverse_bulb_lights_with_the_tail_lamp():
    # MER_S65AMG_12, KOE_Agera_11 (issue #103): a red reflector bowl round the reverse bulb lit as
    # the reverse lamp (it is inside the bowl's outline). On red texels it lights with the red
    # lamp nearest it; a white one stays the reverse lamp's, and with no red lamp near it
    # stays unlit rather than light white on reverse.
    bowl = _strip("taillightL", "textured_reflector", 0.6, -2.02, 4)  # x 0.4 to 0.8
    rev = _quad("revL", "reverse_light", 0.5, -2.0)
    tail = _quad("tailL", "tail_light", 0.75, -2.0)
    assert _seen_functions([bowl, rev, tail], {})["taillightL"][0] == lights.REVERSE
    got = _seen_functions([bowl, rev, tail], {"taillightL": "red"})["taillightL"]
    assert set(got) == {lights.TAIL}, got
    got = _seen_functions([bowl, rev, _quad("tailL", "tail_light", 1.5, -2.0)], {"taillightL": "red"})
    assert set(got["taillightL"]) == {None}, got


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
