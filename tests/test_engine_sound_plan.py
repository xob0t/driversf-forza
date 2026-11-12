"""Engine sound Plan.candidates (issue #594): the order (template, loop length) pairs are
tried in, when a template fails. A failed template's entries leave the order and the next
entry tried is the one that followed it, so no loop length of a later template is skipped
and the last template (the 227 fallback) is reached. build_sound and the template lookup
are stubbed; no game data is read.

uv run python tests/test_engine_sound_plan.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from types import SimpleNamespace

from dsfforza.convert import engine_sound as es


def _tried(templates: list[int], failing: set[int], fail_at: dict[int, float] | None = None):
    """The (template, max_seconds) pairs build_sound is called with when every candidate is
    drawn, and those it built; templates in `failing` raise on every call, those in `fail_at`
    only at that loop length."""
    fail_at = fail_at or {}
    calls = []

    def build_sound(sbak, _vsvh, _forza, *, max_seconds, **_kw):
        calls.append((sbak, max_seconds))
        if sbak in failing or fail_at.get(sbak) == max_seconds:
            raise RuntimeError(f"rebuilt bank invalid ({sbak})")
        return SimpleNamespace(report={}, max_seconds=max_seconds)

    plan = es.Plan(None, None, "m", "FM4", 1, {}, templates, set(), None, "", log=lambda _m: None)
    plan.template = lambda sid: (sid, b"")
    real, es.build_sound = es.build_sound, build_sound
    try:
        built = list(plan.candidates())
    finally:
        es.build_sound = real
    return calls, [(s.report["template"], s.max_seconds) for s in built]


def test_failed_template_does_not_skip_the_next_ones_first_length():
    # the issue's case: 100 raises; 200 must start at the first loop length, 1.1 s
    calls, built = _tried([100, 200, 227], {100})
    assert built == [(t, s) for t in (200, 227) for s in es.MAX_SECONDS]
    assert calls == [(100, 1.1), *built]


def test_fallback_is_reached_after_several_failures():
    # three failing templates in a row used to run off the end before 227
    calls, built = _tried([179, 249, 128, 227], {179, 249, 128})
    assert built == [(227, s) for s in es.MAX_SECONDS]
    assert calls == [(179, 1.1), (249, 1.1), (128, 1.1), *built]


def test_failure_after_a_built_length_keeps_the_order():
    # 100 builds at 1.1 s and fails at 0.8 s: its built candidate stays, 200 starts at 1.1 s
    calls, built = _tried([100, 200], set(), {100: es.MAX_SECONDS[1]})
    assert built == [(100, 1.1)] + [(200, s) for s in es.MAX_SECONDS]
    assert calls == [(100, 1.1), (100, 0.8)] + built[1:]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
