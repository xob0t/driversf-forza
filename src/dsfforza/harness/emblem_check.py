"""Garage card emblems: check every card browser shot of a run against what dsfforza.asi gave each card.

Each card of the garage's card browser has its disc (`Spr3D.unlocked<N>`) at
its own top right corner, where it overlaps the picture of the next card
behind it. dsfforza.asi `[hud] card_emblem` shows the Forza logo there for a
car with `GarageEmblem = 1` and the stock SF disc otherwise
(research/gui-assets.md, "Garage card emblem").

The run needs `[garage] menu_debug=1` and `[hud] emblem_debug=1` in the
plugin's ini and `-MenuKeys` with Right/Left taps in the card browser. For
every Right/Left shot (menu-NN.png) the tool finds, in dsfforza.log, the
highlighted card (`itemBrowserIndex`) and the emblem of each card of the last
car list fill, then classifies the discs on screen at the highlighted card and
its neighbours (-1, +1, +2) as Forza (red) or SF (silver) and compares.
Shots are matched to log lines by time: events.json's `wall` (the guest clock,
as in dsfforza.log). Runs from before run-test.ps1 wrote it get the clock offset
from votes (key presses and car list runs follow their shots), which taps at an
even pace leave nearly tied with one tap off; check such an offset by hand.

    uv run dsff emblem_check %LOCALAPPDATA%/Temp/dsfforza-wsb/runs/<stamp> [--hud out/x/gui/hud.lua]

`--hud` also checks each logged decision against the hud.lua the run used
(Forza logo exactly for its `GarageEmblem = 1` cars, except in a group of 20 cars). Exit code 1 on a mismatch.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# Disc centre and half size (pixels of a 1600 x 900 shot) by offset from the highlighted card.
DISCS = {-1: (262, 582, 60), 0: (705, 405, 45), 1: (915, 315, 28), 2: (1045, 265, 20)}
TIME = re.compile(r"(\d\d):(\d\d):(\d\d\.\d{3}) ")


def classify(pixels, centre: tuple[int, int, int]) -> str:
    """'F' (Forza logo: red), 'S' (SF disc: silver) or '-' (neither) in the box around centre."""
    cx, cy, r = centre
    red = silver = 0
    for y in range(cy - r, cy + r):
        for x in range(cx - r, cx + r):
            R, G, B = pixels[x, y][:3]
            if R > 120 and G < 0.45 * R and B < 0.45 * R:
                red += 1
            elif min(R, G, B) > 140 and max(R, G, B) - min(R, G, B) < 35:
                silver += 1
    n = (2 * r) ** 2
    if red > 0.04 * n and red > silver * 0.5:
        return "F"
    return "S" if silver > 0.04 * n else "-"


def shot_discs(path: Path) -> dict[int, str]:
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.size != (1600, 900):
            im = im.resize((1600, 900))
        pixels = im.load()
        return {offset: classify(pixels, centre) for offset, centre in DISCS.items()}


def parse_log(lines) -> dict:
    """Timed records of dsfforza.log: emblem fills, itemBrowserIndex, car list runs, key presses."""
    out = {"fills": [], "indexes": [], "lists": [], "presses": [], "decisions": {}}
    fill_no = None
    for line in lines:
        m = TIME.match(line)
        if not m:
            continue
        t = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
        if m := re.search(r"hud: emblem: fill (\d+) card (\d+) model (\d+) -> (\w)", line):
            if int(m[1]) != fill_no:
                fill_no = int(m[1])
                out["fills"].append((t, {}))
            emblem = "F" if m[4] == "F" else "S"
            out["fills"][-1][1][int(m[2]) - 1] = (int(m[3]), emblem)
            cars = out["lists"][-1][1]["cars"] if out["lists"] else 0
            out["decisions"][int(m[3])] = (emblem, cars)
        elif m := re.search(r"var itemBrowserIndex = (-?\d+)", line):
            out["indexes"].append((t, int(m[1])))
        elif m := re.search(r"car list: maker (\d+) group \d+, (\d+) cars .*window from car (\d+), page (\S+)", line):
            out["lists"].append((t, {"maker": int(m[1]), "cars": int(m[2]), "first": int(m[3]), "page": m[4]}))
        elif re.search(r"garage: key (right|left) state FF", line):
            out["presses"].append(t)
    return out


def clock_offset(reactions: list[float], shots: list[float]) -> tuple[float, int, int]:
    """Log time minus events.json time, with its votes and the runner-up's.

    Every menu key is tapped right after the shot before it, and the logged key
    press or car list run follows within ~0.1 s. Taps at an even pace vote as
    much for one tap off; the uneven waits (Down, Enter, Esc) decide.
    """
    votes = Counter(round(r - s, 1) for r in reactions for s in shots)

    def score(o):
        return votes[o] + votes[round(o - 0.1, 1)] + votes[round(o + 0.1, 1)]

    best = max(votes, key=score)
    runner = max((o for o in votes if abs(o - best) > 0.5), key=score, default=best)
    return best, score(best), score(runner)


def matches(seen: dict[int, str], expected: dict[int, str] | None) -> bool:
    """The discs on screen match wherever a card is expected."""
    return expected is not None and all(seen[o] == expected[o] for o in DISCS if expected[o] != "-")


def last_before(records: list, t: float):
    best = None
    for record in records:
        if record[0] > t:
            break
        best = record
    return best


def check(run: Path, hud: Path | None = None) -> int:
    events = json.loads((run / "events.json").read_text(encoding="utf-8-sig"))
    log = parse_log((run / "dsfforza.log").read_text(errors="replace").splitlines())
    if not log["fills"] or not log["presses"]:
        print("no emblem_debug fills or menu_debug key presses in dsfforza.log", file=sys.stderr)
        return 2
    walls = all("wall" in e for e in events)
    menu = []
    for e in events:
        if m := re.match(r"menu: (\w+) -> (menu-\d+\.png)", e["message"]):
            # The event is noted after the shot is saved and before the next tap.
            t = TIME.match(e["wall"] + " ") if walls else None
            menu.append((int(t[1]) * 3600 + int(t[2]) * 60 + float(t[3]) - 0.02 if t else e["t"], m[1], m[2]))
    shots = [s for s in menu if s[1] in ("Right", "Left")]
    if walls:
        offset, note = 0.0, "shot times from events.json's guest clock"
    else:
        # Older runs: events.json has seconds since the run started only.
        reactions = log["presses"] + [t for t, _ in log["lists"]]
        offset, votes, runner = clock_offset(reactions, [s[0] for s in menu])
        offset -= 0.3
        note = f"clock offset {offset + 0.3:+.1f} s from {votes} votes (next best {runner}): check it by hand"
    print(f"{len(shots)} Right/Left shots, {len(log['presses'])} logged presses, {note}")
    bad = checked = late = 0
    previous = None  # the last checked shot's expected discs
    for t, key, png in shots:
        at = t + offset
        listed, fill, index = (last_before(log[k], at) for k in ("lists", "fills", "indexes"))
        if not (listed and fill and index) or listed[1]["page"] != "ItemBrowser":
            continue
        i, cards, view = index[1], fill[1], listed[1]
        expected = {o: cards[i + o][1] if (i + o) in cards else "-" for o in DISCS}
        seen = shot_discs(run / png)
        ok = matches(seen, expected)
        # A busy VM can draw a step late: the shot then still shows the previous step.
        # The next shot catches up. A card shift would show in (nearly) every shot.
        lags = not ok and matches(seen, previous)
        checked += 1
        late += lags
        bad += not ok and not lags
        previous = expected
        verdict = "ok" if ok else "LATE (shows the previous step)" if lags else "MISMATCH"
        print(
            f"{png} {key:5} maker {view['maker']} ({view['cars']} cars) car {view['first'] + i + 1:3} "
            f"card {i + 1:2} model {cards.get(i, ('?',))[0]}: "
            f"expected {''.join(expected[o] for o in sorted(DISCS))} "
            f"on screen {''.join(seen[o] for o in sorted(DISCS))} {verdict}"
        )
    print(
        f"{checked} shots checked: {bad} with a disc that differs, {late} drawn a step late "
        "(offsets -1, 0, +1, +2 of the highlighted card)"
    )
    if hud:
        text = hud.read_text(encoding="utf-8")
        forza = {int(m) for m in re.findall(r"\[(\d+)\] = \{[^\n]*GarageEmblem = 1", text)}
        # A group of exactly 20 cars keeps the SF disc: its card 20 needs the spare sprite.
        wrong = sorted(k for k, (e, cars) in log["decisions"].items() if (k in forza and cars != 20) != (e == "F"))
        print(f"{len(log['decisions'])} models seen; decisions that differ from {hud.name}: {wrong or 'none'}")
        bad += bool(wrong)
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run", type=Path, help="run folder (runs\\vm-<stamp>)")
    ap.add_argument("--hud", type=Path, help="the run's hud.lua: check each decision against GarageEmblem")
    args = ap.parse_args()
    return check(args.run, args.hud)


if __name__ == "__main__":
    sys.exit(main())
