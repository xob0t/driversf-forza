"""Which cars the interior glass lint expects crack lines from (port_batch.plain_glass),
from cockpit reports only.

    uv run python tests/test_glass_lint.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.build import port_batch
from dsfforza.convert import glass


def _car(windows: dict | None) -> dict:
    return {"cockpit": {"interior": {"glass_windows": windows}}} if windows is not None else {"cockpit": {}}


def test_windows_with_cracks():
    # BMW_M3E30_91: windscreen and side windows qualified (crack_m), lamps did not
    car = _car(
        {
            "glassf/windscreen": {"slot": "windscreen", "area": 0.767, "crack_m": [0.91, 0.71]},
            "interior/lamp/l": {"slot": "side_front", "area": 0.031},
        }
    )
    assert not port_batch.plain_glass(car)


def test_lamp_covers_only():
    # MAZ_MX5SL_10 (no windscreen), AUD_2_R8_06 (lexan headlight covers): no window qualifies
    car = _car({"interior/lamp/l": {"area": 0.024}, "lexanlhl/lamp/l": {"area": 0.108}})
    assert port_batch.plain_glass(car)


def test_no_converted_cockpit():
    # stub cockpits keep the host's interior: no report, the host comparison decides
    assert not port_batch.plain_glass(_car(None))
    assert not port_batch.plain_glass({})
    assert not port_batch.plain_glass(_car({}))


def test_uv_signature_ignores_order():
    a = [([(0.1, 0.2), (0.3, 0.4), (0.5, 0.6)], None, None), ([(0.7, 0.8), (0.1, 0.1), (0.2, 0.2)], None, None)]
    b = [([(0.2, 0.2), (0.7, 0.8), (0.1, 0.1)], None, None), ([(0.5, 0.6), (0.1, 0.2), (0.3, 0.4)], None, None)]
    assert glass._uv_signature(a) == glass._uv_signature(b)
    assert glass._uv_signature(a) != glass._uv_signature(a[:1])


def test_lexan_windows_take_their_named_slots():
    # Race cars' lexan<suffix> windows are glass<suffix> (#439): the windscreen and door glass
    # go on their slots whatever a triangle faces; groups keep the section's own name.
    assert glass.slot_for("lexanF", (0.0, 1.0, 0.5), (0.0, 1.0, 0.1)) == ("windscreen", "lexanf/windscreen")
    assert glass.slot_for("lexanLF", (0.8, 1.0, 0.2), (0.3, 1.0, 0.0)) == ("side_front", "lexanlf/side_front")
    assert glass.slot_for("lexanR", (0.0, 1.0, -1.0), (0.0, 1.0, -0.1), "glass") == ("rear", "lexanr/rear")
    assert glass.slot_for("lexanLHL", (0.7, 0.4, 1.9), (0.0, 0.0, 1.0)) == ("side_front", "lexanlhl/lamp/l")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
