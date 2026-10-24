"""lights.LampPlan head lamps for cars without a head-lamp technique (synthetic sections).

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


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
