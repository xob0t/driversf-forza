"""dsff emblem_check: dsfforza.log parsing, shot-to-log clock offset, disc colour classes.

uv run python tests/test_emblem_check.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.harness import emblem_check

LOG = """\
12:07:43.422 garage: car list: maker 10 group 9, 46 cars (46 listed), index 1, window from car 0, page ItemBrowser
12:07:43.423 hud: emblem: fill 7 card 1 model 282 -> SF
12:07:43.423 hud: emblem: fill 7 card 2 model 52 -> Forza logo
12:07:43.426 garage: var itemBrowserIndex = 1 (00000001)
12:07:45.331 garage: key right state FF index 1 -> script
12:07:45.333 garage: car list: maker 10 group 9, 46 cars (46 listed), index 2, window from car 0, page ItemBrowser
12:07:45.334 hud: emblem: fill 8 card 1 model 282 -> SF
""".splitlines()


def test_parse_log():
    log = emblem_check.parse_log(LOG)
    assert [f[1] for f in log["fills"]] == [{0: (282, "S"), 1: (52, "F")}, {0: (282, "S")}]
    assert log["indexes"] == [(12 * 3600 + 7 * 60 + 43.426, 1)]
    assert log["presses"] == [12 * 3600 + 7 * 60 + 45.331]
    assert log["lists"][1][1] == {"maker": 10, "cars": 46, "first": 0, "page": "ItemBrowser"}
    assert log["decisions"] == {282: ("S", 46), 52: ("F", 46)}


def test_clock_offset():
    # Down taps every 1.0 s, then Right every 1.9 s: each reaction 0.05 s after a shot.
    shots = [100.0 + 1.0 * k for k in range(10)] + [110.0 + 1.9 * k for k in range(20)]
    reactions = [s + 5000.05 for s in shots[:-1]]
    offset, votes, runner = emblem_check.clock_offset(reactions, shots)
    assert abs(offset - 5000.05) <= 0.06 and votes > runner


def test_classify():
    def box(colour):
        return {(x, y): colour for x in range(20) for y in range(20)}

    centre = (10, 10, 10)
    assert emblem_check.classify(box((220, 40, 30)), centre) == "F"  # Forza red
    assert emblem_check.classify(box((200, 205, 210)), centre) == "S"  # SF silver
    assert emblem_check.classify(box((210, 160, 20)), centre) == "-"  # card gold
    assert emblem_check.classify(box((0, 0, 0)), centre) == "-"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
