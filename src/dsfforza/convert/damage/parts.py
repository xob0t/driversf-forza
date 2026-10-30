"""Forza's own parts along real shut lines: connected components, their stats and spans."""

from __future__ import annotations

import math
from collections import Counter, defaultdict

from dsfforza.convert.damage.core import DOORS

# --- Forza parts along real shut lines ------------------------------------------------
#
# Forza's `body` section is one surface, but its mesh is cut along the real shut
# lines: the vertices at a door's or wing's edge are not shared with the panel
# next to it (the M5's front and rear doors, wings and rear quarters are each a
# component of triangles joined through shared vertex indices; the S5S Raptor's
# door too, while its wings, sills and rear quarters are one shell). The panel
# vote follows the host's layout, so a door patch spilled over those edges and
# had to be held shut (DamagePlan.bound). A door or wing whose triangles are
# mostly whole components that voted for it takes exactly those components: its
# border is then the Forza shut line, and it can swing like vanilla.

SWINGS = DOORS + (33, 34)  # roles that swing on vanilla cars (doors, front wings)
PART_SHARE = 0.6  # share of a component's area its panel vote needs to take it whole
PART_MIN_AREA = 0.15  # m2 of whole components a door or wing needs to be a Forza part


def part_components(keys: list) -> list[int]:
    """Component id per triangle: triangles joined through shared vertex keys."""
    parent: dict = {}

    def find(x):
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    for t, ks in enumerate(keys):
        for k in ks:
            a, b = find(("t", t)), find(k)
            if a != b:
                parent[a] = b
    roots = [find(("t", t)) for t in range(len(keys))]
    ids = {}
    return [ids.setdefault(r, len(ids)) for r in roots]


def _component_votes(labels, areas, comps) -> dict:
    votes = defaultdict(Counter)
    for lab, area, c in zip(labels, areas, comps):
        votes[c][lab] += area
    return votes


def part_stats(labels, areas, comps, swings) -> tuple[Counter, Counter]:
    """Per swinging panel: area of whole components it wins with PART_SHARE,
    and area labelled with it."""
    clean, total = Counter(), Counter()
    for lab, area in zip(labels, areas):
        if lab in swings:
            total[lab] += area
    for votes in _component_votes(labels, areas, comps).values():
        win, w = votes.most_common(1)[0]
        if win in swings and w >= PART_SHARE * sum(votes.values()):
            clean[win] += sum(votes.values())
    return clean, total


def part_relabel(labels, areas, comps, parts: set, fallback) -> list:
    """Panels in `parts` keep exactly the components they win with PART_SHARE;
    their other triangles take the best other panel of their component (or
    `fallback`), and a component won by another panel takes that one whole."""
    if not parts:
        return list(labels)
    votes = _component_votes(labels, areas, comps)
    whole, other = {}, {}
    for c, v in votes.items():
        win, w = v.most_common(1)[0]
        if w >= PART_SHARE * sum(v.values()) and (win in parts or any(lab in parts for lab in v)):
            whole[c] = win
        else:
            others = [lab for lab, _ in v.most_common() if lab not in parts]
            other[c] = others[0] if others else fallback
    return [whole[c] if c in whole else other[c] if lab in parts else lab for lab, c in zip(labels, comps)]


def tear_length(labels, keys, swings) -> float:
    """Length (m) of edges inside one Forza surface (two triangles sharing both
    vertices of the edge) whose two sides are on different panels, one of them
    in `swings`: where a swinging door or wing would tear the surface open.
    keys: per triangle (vertex keys, position keys at 1/2000 m)."""
    owners = defaultdict(list)
    for t, (vk, pk) in enumerate(keys):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            e = (min(vk[a], vk[b]), max(vk[a], vk[b]))
            owners[e].append((t, pk[a], pk[b]))
    total = 0.0
    for ts in owners.values():
        if len(ts) != 2:
            continue
        (t, p, q), (u, _, _) = ts
        if labels[t] != labels[u] and (labels[t] in swings or labels[u] in swings):
            total += math.dist(p, q) / 2000
    return total


PART_MARGIN = 0.05  # m; along z, beyond a part's largest component, that still belongs to it


def part_spans(items, parts: set) -> dict:
    """Part -> (z min, z max, side): the length and side (sign of x) of its
    largest component. items: (tris (sub id, index, centroid), areas,
    components, labels) per section."""
    best = {}
    for n, (_tris, areas, comps, labels) in enumerate(items):
        size = Counter()
        for lab, area, c in zip(labels, areas, comps):
            if lab in parts:
                size[(lab, n, c)] += area
        for (lab, _, c), area in size.items():
            if area > best.get(lab, (0, None))[0]:
                best[lab] = (area, (n, c))
    span = {}
    for lab, (_, (n, c)) in best.items():
        tris, _, comps, _ = items[n]
        pts = [t[2] for t, cc in zip(tris, comps) if cc == c]
        side = 1.0 if sum(p[0] for p in pts) >= 0 else -1.0
        span[lab] = (
            min(p[2] for p in pts),
            max(p[2] for p in pts),
            side,
            min(abs(p[0]) for p in pts),
            min(p[1] for p in pts),
            max(p[1] for p in pts),
        )
    return span


def within_span(c, span) -> bool:
    return span[0] - PART_MARGIN <= c[2] <= span[1] + PART_MARGIN and c[0] * span[2] > 0


CARD_DEPTH = 0.2  # m; inward of a door's skin that its door card (Forza `interior` triangles) may sit


def in_door_card(c, normal, span) -> bool:
    """Is an interior triangle (centroid c) the door card of the door with this
    span: within its length and height, on its side, at most CARD_DEPTH inward
    of its skin, facing into the cabin."""
    return (
        span[0] <= c[2] <= span[1]
        and c[0] * span[2] > 0
        and abs(c[0]) >= span[3] - CARD_DEPTH
        and span[4] - PART_MARGIN <= c[1] <= span[5] + PART_MARGIN
        and normal is not None
        and normal[0] * span[2] < -0.3
    )


def part_trim(tris, areas, comps, labels, span: dict, fallback) -> list:
    """Components on a part that lie mostly (by area) outside its span go to `fallback`."""
    inside, total = Counter(), Counter()
    for t, lab, area, c in zip(tris, labels, areas, comps):
        if lab in span:
            total[(lab, c)] += area
            inside[(lab, c)] += area if within_span(t[2], span[lab]) else 0.0
    drop = {k for k in total if inside[k] < PART_SHARE * total[k]}
    return [fallback if (lab, c) in drop else lab for lab, c in zip(labels, comps)]
