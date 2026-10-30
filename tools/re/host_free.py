"""What a ported car keeps from its host: the UPVH and VO3D field inventory behind
research/host-free.md.

    uv run python tools/re/host_free.py rules [--json out.json]
        Rules over the 150 vanilla hierarchies (record order, table indices, tree fields,
        where lights and markers hang, which undecoded record words a (type, role)
        template would fill, header and table constants): how many vehicles or records
        each holds on, and the exceptions.

    uv run python tools/re/host_free.py diff --build DIR [--json out.json]
        A port_batch build (DIR holds batch-report.json and dngvehicles.sp) against its
        hosts in the vanilla archive: per UPVH field and VO3D parameter, on how many cars
        the port's value differs from its host's. Records are matched by (type, role,
        occurrence), table entries by the matched record's index.

    uv run python tools/re/host_free.py inventory [--rules rules.json] [--diff diff.json]
        The field inventory (FIELDS, VO3D classes from the diff) as a markdown table, and
        the count per class: generated, adjusted, copied, copied blind.

Read-only on both archives; no converter code is changed or run.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import struct
import sys
from collections import Counter, defaultdict
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import handling as H
from dsfforza.convert.damage import _tree_fields
from dsfforza.formats import upvh as U
from dsfforza.formats.chnk import read_chunk
from dsfforza.formats.vehicle_mesh import child, payload
from dsfforza.formats.vehicles import find, parse_sslp

VANILLA = REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"

# Record words not decoded in research/upvh.md (offset, struct format). 0x0C's bits 0 and
# 1 are decoded; the word is checked whole and as bits 2..15.
UNDECODED = [
    ("0x0C bits 2..15", 0x0C, "<H"),
    ("0x0E", 0x0E, "<H"),
    ("0x10", 0x10, "<H"),
    ("0x12", 0x12, "<H"),
    ("0x24", 0x24, "<H"),
    ("0x28", 0x28, "<H"),
    ("0x2C", 0x2C, "<H"),
    ("0x2E", 0x2E, "<H"),
    ("0x32", 0x32, "<H"),
    ("0x36", 0x36, "<H"),
    ("0x38", 0x38, "<H"),
    ("0x3A", 0x3A, "<H"),
    ("0x3C", 0x3C, "<H"),
    ("0x44", 0x44, "<I"),
    ("0x48", 0x48, "<I"),
    ("0x4C", 0x4C, "<I"),
]
INDEX_FIELDS = ["marker", "hinge", "body", "seat", "mirror", "light", "lod_link", "attachment", "wheel"]
TABLE_OF = {
    "marker": "markers",
    "hinge": "hinges",
    "body": "bodies",
    "seat": "seats",
    "mirror": "mirrors",
    "light": "lights",
    "lod_link": "lod_links",
    "attachment": "attachments",
    "wheel": "wheels",
}


# --- archive access -------------------------------------------------------------------


def vehicles(path: Path, ids=None):
    """(spool id, UPVH, VO3D, full MODL, exterior MODL) of every vehicle's full entry."""
    with path.open("rb") as stream:
        root = read_chunk(stream, 0)
        ssic = find(root.entries, "SSIC")
        sslp = parse_sslp(payload(stream, find(find(ssic.children, "NONR").children, "SSLP")))
        by_id = {sid: index for sid, index, _ in sslp}
        for sid, index, kind in sorted(sslp):
            if kind != 0 or (ids is not None and sid not in ids):
                continue
            nonr = child(root.entries[index], "NONR")
            ext = by_id.get(sid + 2000)
            yield (
                sid,
                payload(stream, child(nonr, "UPVH")),
                payload(stream, child(nonr, "VO3D")),
                payload(stream, child(nonr, "MODL")),
                payload(stream, child(child(root.entries[ext], "NONR"), "MODL")) if ext is not None else None,
            )


def parents(h) -> list[int | None]:
    out, stack = [None] * len(h.records), []
    for r in h.records:
        while stack and stack[-1][1] <= r.index:
            stack.pop()
        out[r.index] = stack[-1][0] if stack else None
        stack.append((r.index, r.index + r.subtree))
    return out


def children(h, i: int) -> list[int]:
    out, c = [], i + 1
    while c < i + h.records[i].subtree:
        out.append(c)
        c += h.records[c].subtree
    return out


class Rule:
    """One rule's tally: on how many items it holds, and some exceptions."""

    def __init__(self, name: str, unit: str = "vehicles"):
        self.name, self.unit, self.ok, self.n, self.bad = name, unit, 0, 0, []

    def check(self, ok: bool, where) -> None:
        self.n += 1
        self.ok += bool(ok)
        if not ok and len(self.bad) < 12:
            self.bad.append(where)

    def row(self) -> dict:
        return {"rule": self.name, "holds": self.ok, "of": self.n, "unit": self.unit, "exceptions": self.bad}


# --- rules over the vanilla fleet ------------------------------------------------------


def vanilla_rules(path: Path) -> dict:
    rules: dict[str, Rule] = {}

    def rule(name, unit="vehicles") -> Rule:
        if name not in rules:
            rules[name] = Rule(name, unit)
        return rules[name]

    light_parent = Counter()  # (end, b3 group, parent type, parent role)
    light_parent_role = Counter()
    marker_parent = defaultdict(Counter)  # marker role -> parent role
    words = defaultdict(lambda: defaultdict(Counter))  # word -> (type, role) -> value
    words_type = defaultdict(lambda: defaultdict(Counter))  # word -> type -> value
    hinge_kinds = defaultdict(Counter)  # role -> (limit, param)
    slotless = Counter()
    slotted = Counter()
    sibling_orders = Counter()
    light_counts = []
    for sid, data, vo3d, modl, ext_modl in vehicles(path):
        h = U.parse(data)
        recs = h.records
        par = parents(h)
        n = len(recs)
        v = f"{sid}"
        # Header
        rule("header +0x0C type = 26").check(h.type == 26, v)
        rule("header +0x14 hierarchy id = spool id").check(h.hierarchy_id == sid, v)
        rule("header +0x04 model UID = MODL part 0").check(h.model_uid == U.modl_part_uids(modl)[0], v)
        rule("header +0x34 = 0x0CE8").check(struct.unpack_from("<H", data, 0x34)[0] == 0x0CE8, v)
        rule("header +0x54 = ecf31c3d 4dcafb46 82e5e6a5").check(data[0x54:0x60].hex() == "ecf31c3d4dcafb4682e5e6a5", v)
        rule("header +0x38..0x4F all 0xFF").check(data[0x38:0x50] == b"\xff" * 24, v)
        rule("header +0x1C..0x25 all 0").check(data[0x1C:0x26] == bytes(10), v)
        rule("header +0x2E u32 = animation blob size / 4 (0 without one)").check(
            struct.unpack_from("<I", data, 0x2E)[0] * 4 == h.counts["anim"], (v, data[0x2E:0x32].hex())
        )
        # Tree shape
        rule("record 0 is the only root (type 4), subtree = record count").check(
            recs[0].type == 4 and recs[0].subtree == n and sum(r.type == 4 for r in recs) == 1, v
        )
        kids0 = children(h, 0)
        rule("root's children: chassis records (type 5), then one role-38 part holding the rest").check(
            all(recs[k].type == 5 for k in kids0[:-1])
            and recs[kids0[-1]].type == 1
            and recs[kids0[-1]].role == 38
            and kids0[-1] + recs[kids0[-1]].subtree == n,
            (v, [(recs[k].type, recs[k].role) for k in kids0]),
        )
        rule("chassis records (type 5) are leaves").check(all(r.subtree == 1 for r in recs if r.type == 5), v)
        rule("markers, lights and seats (types 3, 17, 18) are leaves").check(
            all(r.subtree == 1 for r in recs if r.type in (3, 17, 18)), v
        )
        rule("records with a part UID are exactly types 1, 2, 5").check(
            all((r.uid is not None) == (r.type in (1, 2, 5)) for r in recs), v
        )
        rule("every record UID is a full-entry MODL part").check(
            all(r.uid in U.modl_part_uids(modl) for r in recs if r.uid), v
        )
        # Table indices in record order
        for f in INDEX_FIELDS:
            vals = [getattr(r, f) for r in recs if getattr(r, f) >= 0]
            count = h.counts[TABLE_OF[f]]
            rule(f"{f} indices: 0..n-1 once each, in record order").check(vals == list(range(count)), (v, vals[:12]))
        cols = [(r.index, r.collision) for r in recs if r.collision >= 0]
        rule("collision offsets fall in record order, root's model last in the PDL").check(
            all(a[1] > b[1] for a, b in itertools.pairwise(cols[1:]))
            and (not cols or cols[0][0] == 0)
            and (len(cols) < 2 or cols[-1][1] == 0),
            v,
        )
        pdl = h.pdl_offset
        one, count, size, sixteen = struct.unpack_from("<4I", data, pdl)
        rule("PDL header {1, model count, PDL size - 0x10, 0x10}").check(
            one == 1 and sixteen == 0x10 and size == h.pdl_size - 0x10, (v, one, size, sixteen)
        )
        rule("PDL model count = records with a collision model").check(count == len(cols), (v, count, len(cols)))
        # Running numbers
        models = [r.model for r in recs if r.type == 1]
        rule("+0x2A over part records (type 1): from 0 in record order, steps of 1").check(
            models == list(range(len(models))), (v, models[:20])
        )
        rule("+0x2A over part records: from 0 in record order, steps of 0 or 1 (shared indices)").check(
            models[:1] in ([], [0]) and all(b - a in (0, 1) for a, b in itertools.pairwise(models)), (v, models[:20])
        )
        rule("+0x2A on wheel records = 0").check(all(r.model == 0 for r in recs if r.type == 2), v)
        slots = [r.slot for r in recs if r.type == 1 and r.slot >= 0]
        rule("+0x34 over part records: 0.. in record order (some parts have none)").check(
            slots == list(range(len(slots))), (v, slots[:20])
        )
        for r in recs:
            if r.type == 1:
                (slotted if r.slot >= 0 else slotless)[r.role] += 1
        tf = _tree_fields([r.raw for r in recs])
        for r, fields in zip(recs, tf, strict=True):
            got = (struct.unpack_from("<H", r.raw, 0x0C)[0] & 1,) + struct.unpack_from("<HH", r.raw, 0x16)
            rule("+0x0C bit 0, +0x16, +0x18 = _tree_fields", "records").check(got == fields, (v, r.index, r.role))
            wheel_below = any(x.type == 2 for x in recs[r.index + 1 : r.index + r.subtree])
            rule("+0x0C bit 1 = a wheel record below this one", "records").check(
                bool(struct.unpack_from("<H", r.raw, 0x0C)[0] & 2) == wheel_below, (v, r.index)
            )
            rule("+0x0C bit 4 only on the root", "records").check(
                bool(struct.unpack_from("<H", r.raw, 0x0C)[0] & 0x10) == (r.type == 4), (v, r.index)
            )
            for name, off, fmt in UNDECODED:
                val = struct.unpack_from(fmt, r.raw, off)[0]
                if name.startswith("0x0C"):
                    val &= ~0x13
                words[name][(r.type, r.role)][val] += 1
                words_type[name][r.type][val] += 1
        # Sibling order: per parent, the child types in order, light/seat runs collapsed
        for r in recs:
            kids = children(h, r.index)
            if kids:
                seq = []
                for k in kids:
                    t = {17: "L", 18: "S", 3: "M", 1: "P", 2: "W", 5: "C"}.get(recs[k].type, "?")
                    if not seq or seq[-1] != t:
                        seq.append(t)
                sibling_orders["".join(seq)] += 1
                lights_first = all(recs[k].type == 17 for k in kids[: sum(recs[k].type == 17 for k in kids)])
                rule("a record's light children come before its other children", "parents").check(
                    lights_first, (v, r.index)
                )
                seats = [k for k in kids if recs[k].type == 18]
                if seats:
                    rest = [k for k in kids if recs[k].type != 17]
                    rule("seat (gauge) children come right after the lights", "parents").check(
                        rest[: len(seats)] == seats, (v, r.index)
                    )
        # Lights
        light_counts.append(h.counts["lights"])
        table = h.table("lights")
        for i, e in enumerate(table):
            a, b = struct.unpack_from("<2f", e, 0x20)
            rule("light +0x20, +0x24 = pi, pi", "entries").check(
                math.isclose(a, math.pi, rel_tol=1e-6) and math.isclose(b, math.pi, rel_tol=1e-6), (v, i)
            )
            rule("light +0x30..0x3F the same in every entry of the vehicle", "entries").check(
                e[0x30:] == table[0][0x30:], (v, i)
            )
            words["light +0x30"][("light", 0)][e[0x30:].hex()] += 1
            rule("light w of position = 1, of direction = 0", "entries").check(
                struct.unpack_from("<f", e, 12)[0] == 1.0 and struct.unpack_from("<f", e, 28)[0] == 0.0,
                (v, i, struct.unpack_from("<f", e, 12)[0], struct.unpack_from("<f", e, 28)[0]),
            )
        ids = [struct.unpack_from("<I", e, 0x28)[0] for e in table]
        rule("light ids: a permutation of 0..n-1").check(sorted(ids) == list(range(len(ids))), v)
        rule("light id = table index").check(ids == list(range(len(ids))), (v, ids))
        rule("lights: one type-17 record per table entry").check(
            sorted(r.light for r in recs if r.type == 17) == list(range(len(table))), v
        )
        for r in recs:
            if r.type == 17 and r.light >= 0:
                e = table[r.light]
                z = struct.unpack_from("<f", e, 8)[0]
                p = recs[par[r.index]]
                end = "front" if z > 0 else "rear"
                light_parent[(end, p.type, p.role)] += 1
                light_parent_role[p.role] += 1
                rule("a light's parent is a part record (type 1)", "lights").check(p.type == 1, (v, r.index, p.type))
        # Markers
        for r in recs:
            if r.type == 3:
                p = recs[par[r.index]]
                marker_parent[r.role][(p.type, p.role)] += 1
        # Hinges by role
        hinges = h.table("hinges")
        for r in recs:
            if r.hinge >= 0:
                limit, param = struct.unpack_from("<2f", hinges[r.hinge], 0x20)
                hinge_kinds[(r.type, r.role)][(round(limit, 4), round(param, 3))] += 1
        for i, e in enumerate(hinges):
            rule("hinge +0x28..0x2F zero, pivot w 1, axis w 0", "entries").check(
                e[0x28:] == bytes(8)
                and struct.unpack_from("<f", e, 12)[0] == 1.0
                and struct.unpack_from("<f", e, 28)[0] == 0.0,
                (v, i),
            )
        for i, e in enumerate(h.table("bodies")):
            m4 = struct.unpack_from("<16f", e, 0x50)
            ident = tuple(float(i == j) for i in range(4) for j in range(4))
            rule("body +0x50 4x4 = identity, +0x90..0xAF zero", "entries").check(
                all(math.isclose(a, b, abs_tol=1e-6) for a, b in zip(m4, ident)) and e[0x90:] == bytes(0x20),
                (v, i),
            )
        for i, e in enumerate(h.table("seats")):
            rule("seat (gauge) +0x29..0x2F zero, id 1 or 2", "entries").check(
                e[0x29:] == bytes(7) and struct.unpack_from("<I", e, 0x20)[0] in (1, 2), (v, i)
            )
        wheels = h.table("wheels")
        for i, e in enumerate(wheels):
            x = struct.unpack_from("<f", e)[0]
            q = struct.unpack_from("<4f", e, 16)
            want = (0.0, 1.0, 0.0, 0.0) if x < 0 else (0.0, 0.0, 0.0, 1.0)
            rule("wheel quaternion (0,1,0,0) at -x, identity at +x", "entries").check(
                all(math.isclose(a, b, abs_tol=1e-4) for a, b in zip(q, want)), (v, i, q)
            )
        links = h.lod_links()
        ext_ids = set(U.modl_part_uids(ext_modl)) if ext_modl else set()
        for r in recs:
            if r.lod_link >= 0:
                rule("records with a LOD link: types 1, 2, 5", "records").check(r.type in (1, 2, 5), (v, r.index))
        rule("every exterior LOD link resolves to an exterior MODL part").check(
            all(e is None or e in ext_ids for _, e in links), v
        )
        for i, e in enumerate(h.table("attachments")):
            uid = U.swap_uid(e[0x10:0x18])
            rule("attachment +0x10 = an exterior MODL part UID", "entries").check(uid in ext_ids, (v, i))
        att = [r for r in recs if r.attachment >= 0]
        rule("attachments hang on marker records (type 3)").check(
            all(r.type == 3 for r in att) or not att, (v, [(r.type, r.role) for r in att])
        )
        for r in att:
            rule("attachment markers: roles 85, 86", "records").check(r.role in (85, 86), (v, r.index, r.role))
        # VO3D
        rule("VO3D +0 spool id = hierarchy id").check(struct.unpack_from("<H", vo3d)[0] == sid, v)
    out = {"rules": [r.row() for r in rules.values()]}
    # Undecoded record words: does (type, role) or type alone fix the value?
    det = []
    for name, groups in words.items():
        if name == "light +0x30":
            continue
        total = sum(sum(c.values()) for c in groups.values())
        by_role = sum(c.most_common(1)[0][1] for c in groups.values())
        by_type = sum(c.most_common(1)[0][1] for c in words_type[name].values())
        values = Counter()
        for c in groups.values():
            values.update(c)
        det.append(
            {
                "word": name,
                "records": total,
                "mode_of_type": by_type,
                "mode_of_type_role": by_role,
                "distinct": len(values),
                "top": [[f"{k:#x}", n] for k, n in values.most_common(5)],
            }
        )
    out["undecoded_words"] = det
    out["light_junk"] = words["light +0x30"][("light", 0)].most_common(3)
    out["light_parents"] = [[*k, n] for k, n in light_parent.most_common(30)]
    out["light_parent_roles"] = light_parent_role.most_common(20)
    out["marker_parents"] = {
        str(role): [[*k, n] for k, n in c.most_common(4)]
        for role, c in sorted(marker_parent.items(), key=lambda kv: -sum(kv[1].values()))
    }
    out["hinge_kinds"] = {
        f"{t}/{role}": [[*k, n] for k, n in c.most_common(3)]
        for (t, role), c in sorted(hinge_kinds.items(), key=lambda kv: -sum(kv[1].values()))
    }
    out["slotless_roles"] = {str(k): [slotless[k], slotted[k]] for k in sorted(slotless, key=lambda k: -slotless[k])}
    out["sibling_orders"] = sibling_orders.most_common(25)
    out["light_counts"] = {"min": min(light_counts), "max": max(light_counts), "median": sorted(light_counts)[75]}
    return out


def print_rules(out: dict) -> None:
    for r in out["rules"]:
        extra = f"  e.g. {r['exceptions'][:4]}" if r["holds"] != r["of"] else ""
        print(f"{r['holds']:6d} / {r['of']:<6d} {r['unit']:9s} {r['rule']}{extra}")
    print("\nundecoded record words: records whose value is the mode of their type / (type, role)")
    for d in out["undecoded_words"]:
        print(
            f"  {d['word']:16s} {d['mode_of_type']:6d} / {d['mode_of_type_role']:6d} of {d['records']}"
            f"  ({d['distinct']} values; top {d['top'][:3]})"
        )
    for key in (
        "light_junk",
        "light_parent_roles",
        "light_parents",
        "slotless_roles",
        "sibling_orders",
        "light_counts",
    ):
        print(f"\n{key}: {out[key]}")
    print("\nmarker parents (role: [(parent type, parent role, n)]):")
    for role, rows in list(out["marker_parents"].items())[:25]:
        print(f"  {role}: {rows}")
    print("\nhinge kinds ((type/role): [(limit, param, n)]):")
    for role, rows in list(out["hinge_kinds"].items())[:25]:
        print(f"  {role}: {rows}")


# --- a build against its hosts ---------------------------------------------------------

PANEL_ROLES = {31, 32, 33, 34, 52, 53, 54, 55, 4, 5, 124, 35, 36, 50, 51, 40, 41, 15, 136, 8, 37, 38, 0}


def role_class(r) -> str:
    """What the converter does with a record's tables depends on its kind: wheels
    (patch_hubs), the steering wheel and gauge cluster (cockpit), panels (damage)..."""
    if r.type == 2:
        return "wheel"
    if r.type == 5:
        return "chassis"
    if r.type == 4:
        return "root"
    if r.type == 17:
        return "light"
    if r.type == 18:
        return "gauge"
    if r.type == 3:
        return {47: "steering", 10: "seat", 11: "seat", 77: "exhaust", 85: "attach", 86: "attach"}.get(r.role, "other")
    if r.role == 1:
        return "steering"
    if r.role == 185:
        return "cluster"
    if r.role in (188, 190, 155, 156):
        return "suspension"
    return "panel" if r.role in PANEL_ROLES else "other"


# Table entry fields: (table, name, offset, struct format).
TABLE_FIELDS = [
    ("hinges", "pivot", 0x00, "<3f"),
    ("hinges", "axis", 0x10, "<3f"),
    ("hinges", "limit", 0x20, "<f"),
    ("hinges", "param", 0x24, "<f"),
    ("bodies", "rotation", 0x00, "<12f"),
    ("bodies", "position", 0x30, "<3f"),
    ("bodies", "inertia", 0x40, "<3f"),
    ("bodies", "mass", 0x4C, "<f"),
    ("bodies", "matrix", 0x50, "<16f"),
    ("markers", "position", 0x00, "<3f"),
    ("markers", "quaternion", 0x10, "<4f"),
    ("seats", "pivot", 0x00, "<3f"),
    ("seats", "quaternion", 0x10, "<4f"),
    ("seats", "gauge id", 0x20, "<I"),
    ("seats", "sweep", 0x24, "<I"),
    ("seats", "hide flag", 0x28, "<B"),
    ("mirrors", "position", 0x00, "<3f"),
    ("lights", "position", 0x00, "<3f"),
    ("lights", "direction", 0x10, "<3f"),
    ("lights", "pi pi", 0x20, "<2f"),
    ("lights", "id", 0x28, "<I"),
    ("lights", "flags b0 variant", 0x2C, "<B"),
    ("lights", "flags b1", 0x2D, "<B"),
    ("lights", "flags b2 colour", 0x2E, "<B"),
    ("lights", "flags b3 type", 0x2F, "<B"),
    ("lights", "junk", 0x30, "<4f"),
    ("attachments", "position", 0x00, "<3f"),
    ("attachments", "part uid", 0x10, "<Q"),
    ("attachments", "junk", 0x18, "<Q"),
    ("wheels", "hub", 0x00, "<3f"),
    ("wheels", "quaternion", 0x10, "<4f"),
]
RECORD_FIELDS = [
    ("+0x0C bits 0,1 (tree)", 0x0C, "<H", 0x3),
    ("+0x0C bits 2..15", 0x0C, "<H", 0xFFFC),
    ("+0x0E", 0x0E, "<H", None),
    ("+0x10", 0x10, "<H", None),
    ("+0x12", 0x12, "<H", None),
    ("+0x14 subtree", 0x14, "<H", None),
    ("+0x16/+0x18 hinge pops", 0x16, "<I", None),
    ("+0x1A marker index", 0x1A, "<h", None),
    ("+0x1C hinge index", 0x1C, "<h", None),
    ("+0x1E body index", 0x1E, "<h", None),
    ("+0x20 collision offset", 0x20, "<i", None),
    ("+0x24, +0x28, +0x2E, +0x32..0x3D (-1/0 fill)", 0x24, "<H", None),
    ("+0x26 seat index", 0x26, "<b", None),
    ("+0x27 mirror index", 0x27, "<b", None),
    ("+0x2A panel-data index", 0x2A, "<h", None),
    ("+0x2C", 0x2C, "<H", None),
    ("+0x30 light index", 0x30, "<h", None),
    ("+0x34 transform slot", 0x34, "<h", None),
    ("+0x3E LOD-link index", 0x3E, "<h", None),
    ("+0x40 attachment index", 0x40, "<h", None),
    ("+0x42 wheel index", 0x42, "<h", None),
    ("+0x44 junk", 0x44, "<12s", None),
]
HEADER_FIELDS = [
    ("model UID", 0x04, "<Q"),
    ("type", 0x0C, "<I"),
    ("record count", 0x10, "<I"),
    ("hierarchy id", 0x14, "<I"),
    ("PDL size", 0x18, "<I"),
    ("anim size", 0x26, "<H"),
    ("marker count", 0x28, "<H"),
    ("hinge count", 0x2A, "<B"),
    ("body count", 0x2B, "<B"),
    ("seat count", 0x2C, "<B"),
    ("mirror count", 0x2D, "<B"),
    ("light count", 0x32, "<H"),
    ("+0x34", 0x34, "<H"),
    ("LOD-link count", 0x36, "<H"),
    ("attachment count", 0x50, "<H"),
    ("wheel count", 0x52, "<H"),
    ("+0x54 constant", 0x54, "<12s"),
]


def _same(a, b) -> bool:
    if isinstance(a, tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6) or (math.isnan(a) and math.isnan(b))
    return a == b


def _match(host, port) -> list[tuple]:
    """(host record, port record) pairs by (type, role, occurrence); unmatched get None."""
    groups = defaultdict(lambda: ([], []))
    for r in host.records:
        groups[(r.type, r.role)][0].append(r)
    for r in port.records:
        groups[(r.type, r.role)][1].append(r)
    out = []
    for a, b in groups.values():
        for k in range(max(len(a), len(b))):
            out.append((a[k] if k < len(a) else None, b[k] if k < len(b) else None))
    return out


def _collision(h, r) -> list[tuple] | None:
    if r.collision < 0:
        return None
    at = h.pdl_offset + 0x10 + r.collision
    n = struct.unpack_from("<I", h.data, at + 12)[0]
    radius = struct.unpack_from("<f", h.data, at + 8)[0]
    prims = [struct.unpack_from("<24f", h.data, at + 0x10 + k * 0x60) for k in range(n)]
    return [(radius,), *prims]


def compare(host_data: bytes, port_data: bytes, tally) -> None:
    """tally(key, changed) for every field of one port against its host."""
    hh, hp = U.parse(host_data), U.parse(port_data)
    for name, off, fmt in HEADER_FIELDS:
        tally(
            f"header {name}",
            not _same(struct.unpack_from(fmt, host_data, off), struct.unpack_from(fmt, port_data, off)),
        )
    for name, _ in U.TABLES:
        tally(f"count {name}", hh.counts[name] != hp.counts[name])
    pairs = _match(hh, hp)
    for a, b in pairs:
        r = a or b
        cls = f"{U.RECORD_TYPES.get(r.type, r.type)}/{role_class(r)}"
        if a is None or b is None:
            tally(f"records {cls}: added or dropped", True)
            continue
        tally(f"records {cls}: matched", False)
        for name, off, fmt, mask in RECORD_FIELDS:
            x, y = struct.unpack_from(fmt, a.raw, off)[0], struct.unpack_from(fmt, b.raw, off)[0]
            if mask is not None:
                x, y = x & mask, y & mask
            if off == 0x24:  # the fill words together
                x = (a.raw[0x24:0x26], a.raw[0x28:0x2A], a.raw[0x2E:0x30], a.raw[0x32:0x34], a.raw[0x36:0x3E])
                y = (b.raw[0x24:0x26], b.raw[0x28:0x2A], b.raw[0x2E:0x30], b.raw[0x32:0x34], b.raw[0x36:0x3E])
            tally(f"record {name} [{cls}]", x != y)
        for table, fname, off, fmt in TABLE_FIELDS:
            field = {
                "hinges": "hinge",
                "bodies": "body",
                "markers": "marker",
                "seats": "seat",
                "mirrors": "mirror",
                "lights": "light",
                "attachments": "attachment",
                "wheels": "wheel",
            }[table]
            i, j = getattr(a, field), getattr(b, field)
            if i < 0 and j < 0:
                continue
            if i < 0 or j < 0:
                tally(f"{table} {fname} [{role_class(r)}]", True)
                continue
            x = struct.unpack_from(fmt, hh.table(table)[i], off)
            y = struct.unpack_from(fmt, hp.table(table)[j], off)
            tally(f"{table} {fname} [{role_class(r)}]", not _same(x, y))
        ca, cb = _collision(hh, a), _collision(hp, b)
        if ca is not None or cb is not None:
            tally(f"PDL model [{role_class(r)}]", ca is None or cb is None or not _same(tuple(ca), tuple(cb)))
        if a.lod_link >= 0 or b.lod_link >= 0:
            # Part UIDs are renewed on every clone: compare whether the link exists.
            tally(f"lod_links present [{role_class(r)}]", (a.lod_link >= 0) != (b.lod_link >= 0))


def vo3d_params(data: bytes) -> dict[int, float]:
    return H.VO3D.parse(data).params


def build_diff(build: Path, vanilla: Path, limit: int | None = None) -> dict:
    report = json.loads((build / "batch-report.json").read_text())
    cars = {c["spool_id"]: c["host"] for c in report["ok"]}
    if limit:
        cars = dict(list(cars.items())[:limit])
    hosts = {sid: (upvh, vo3d) for sid, upvh, vo3d, _, _ in vehicles(vanilla, set(cars.values()))}
    # key -> [cars changed, cars present, entries changed, entries]
    upvh_tally = defaultdict(lambda: [0, 0, 0, 0])
    seen_in_car: dict[str, list] = {}

    def tally(key, changed) -> None:
        row = seen_in_car.setdefault(key, [False, 0, 0])
        row[0] = row[0] or changed
        row[1] += bool(changed)
        row[2] += 1

    vo3d = defaultdict(Counter)
    for sid, upvh, port_vo3d, _, _ in vehicles(build / "dngvehicles.sp", set(cars)):
        host_upvh, host_vo3d = hosts[cars[sid]]
        seen_in_car.clear()
        compare(host_upvh, upvh, tally)
        for key, (changed, entries_changed, entries) in seen_in_car.items():
            row = upvh_tally[key]
            row[0] += changed
            row[1] += 1
            row[2] += entries_changed
            row[3] += entries
        hp, pp = vo3d_params(host_vo3d), vo3d_params(port_vo3d)
        for pid in set(hp) | set(pp):
            c = vo3d[pid]
            c["cars"] += 1
            if pid not in hp:
                c["added"] += 1
            elif pid not in pp:
                c["removed"] += 1
            elif not H.same_f32(hp[pid], pp[pid]):
                c["changed"] += 1
    return {
        "cars": len(cars),
        "upvh": dict(sorted(upvh_tally.items())),
        "vo3d": {str(pid): dict(c) for pid, c in sorted(vo3d.items())},
    }


def print_diff(out: dict) -> None:
    print(
        f"{out['cars']} cars: UPVH fields, cars where the port differs from its host / cars that have the field;"
        " entries (records, table entries) changed / entries"
    )
    for key, (changed, n, e_changed, e) in out["upvh"].items():
        print(f"  {changed:4d} / {n:<4d} {e_changed:6d} / {e:<6d} {key}")
    print("VO3D parameters: changed, added, removed / cars with the parameter on either side")
    for pid, c in out["vo3d"].items():
        moved = c.get("changed", 0) + c.get("added", 0) + c.get("removed", 0)
        print(
            f"  p{pid:<4s} {H.name(int(pid)):28s} {moved:4d} / {c['cars']:<4d}"
            f" (changed {c.get('changed', 0)}, added {c.get('added', 0)}, removed {c.get('removed', 0)})"
        )


# --- the inventory ---------------------------------------------------------------------

GENERATED, ADJUSTED, COPIED, BLIND = "generated", "adjusted", "copied", "copied blind"

# (group, field, class, what writes it, meaning and evidence, vanilla rules (rules
# names), diff keys (build_diff names)). One row per field, split where the converter
# treats records of different kinds differently.
FIELDS = [
    # header
    ("header", "+0x00 magic AWHF", COPIED, "", "format magic", [], []),
    (
        "header",
        "+0x04 model UID",
        GENERATED,
        "clone_vehicle.renew_uids",
        "MODL part 0's UID",
        ["header +0x04 model UID = MODL part 0"],
        ["header model UID"],
    ),
    (
        "header",
        "+0x0C type 26",
        BLIND,
        "",
        "meaning unknown, the same everywhere",
        ["header +0x0C type = 26"],
        ["header type"],
    ),
    (
        "header",
        "+0x10 record count",
        ADJUSTED,
        "exhaust, cockpit.add_gauge_records",
        "records in the list",
        [],
        ["header record count"],
    ),
    (
        "header",
        "+0x14 hierarchy id",
        GENERATED,
        "clone_vehicle.patch_ids",
        "spool id; the VO3D is found by it",
        ["header +0x14 hierarchy id = spool id"],
        ["header hierarchy id"],
    ),
    (
        "header",
        "+0x18 PDL size",
        COPIED,
        "",
        "collision blob size (models edited in place)",
        ["PDL header {1, model count, PDL size - 0x10, 0x10}"],
        ["header PDL size"],
    ),
    ("header", "+0x1C..0x25 zero", BLIND, "", "", ["header +0x1C..0x25 all 0"], []),
    (
        "header",
        "+0x26 animation size, +0x2E animation count, animation blob",
        BLIND,
        "",
        "13 vehicles (active aero?); undecoded",
        ["header +0x2E u32 = animation blob size / 4 (0 without one)"],
        ["count anim"],
    ),
    (
        "header",
        "marker, seat (gauge) counts",
        ADJUSTED,
        "exhaust, cockpit.add_gauge_records",
        "table sizes",
        [],
        ["count markers", "count seats"],
    ),
    (
        "header",
        "light count",
        COPIED,
        "",
        "the host's light entries, whatever the Forza car's lamps",
        [],
        ["count lights"],
    ),
    (
        "header",
        "hinge, body, mirror, LOD-link, attachment, wheel counts",
        COPIED,
        "",
        "table sizes",
        [],
        ["count hinges", "count bodies", "count mirrors", "count lod_links", "count attachments", "count wheels"],
    ),
    (
        "header",
        "+0x34 0x0CE8, +0x54 12 constant bytes",
        BLIND,
        "",
        "not 192, 257",
        ["header +0x34 = 0x0CE8", "header +0x54 = ecf31c3d 4dcafb46 82e5e6a5"],
        ["header +0x34", "header +0x54 constant"],
    ),
    ("header", "+0x38..0x4F 0xFF", BLIND, "", "", ["header +0x38..0x4F all 0xFF"], []),
    # records
    (
        "records",
        "the record set: which parts, markers, lights, gauges, in what order",
        COPIED,
        "",
        "the host's hierarchy; record order fixes every table index below",
        [
            "record 0 is the only root (type 4), subtree = record count",
            "root's children: chassis records (type 5), then one role-38 part holding the rest",
            "markers, lights and seats (types 3, 17, 18) are leaves",
            "a record's light children come before its other children",
            "seat (gauge) children come right after the lights",
        ],
        [],
    ),
    (
        "records",
        "+0x00 part UID",
        GENERATED,
        "renew_uids, remap_upvh_parts, split_rear_wheel",
        "MODL part, halves swapped",
        ["records with a part UID are exactly types 1, 2, 5", "every record UID is a full-entry MODL part"],
        [],
    ),
    ("records", "+0x08 type", COPIED, "", "root, chassis, part, wheel, marker, light, seat", [], []),
    ("records", "+0x0A role (parts)", COPIED, "", "panel kind (damage.md roles table)", [], []),
    (
        "records",
        "+0x0A role (markers)",
        BLIND,
        "",
        "47, 10, 11, 77 decoded; 92, 63/64, 65/67, 69/70, 71..76, 81, 46, 85/86 ... not",
        [],
        [],
    ),
    (
        "records",
        "+0x0C bits 0, 1",
        ADJUSTED,
        "damage.move_subtree, add_gauge_records",
        "a part child; a wheel below",
        ["+0x0C bit 0, +0x16, +0x18 = _tree_fields", "+0x0C bit 1 = a wheel record below this one"],
        ["record +0x0C bits 0,1 (tree)"],
    ),
    (
        "records",
        "+0x0C bit 2 (bit 4: the root)",
        BLIND,
        "",
        "on most part records with a body",
        ["+0x0C bit 4 only on the root"],
        ["record +0x0C bits 2..15"],
    ),
    (
        "records",
        "+0x0E, +0x10, +0x12",
        BLIND,
        "",
        "flags; (type, role) fixes 96-97% of records",
        [],
        ["record +0x0E", "record +0x10", "record +0x12"],
    ),
    (
        "records",
        "+0x14 subtree size",
        ADJUSTED,
        "exhaust, damage remounts, gauges",
        "depth-first tree",
        [],
        ["record +0x14 subtree"],
    ),
    (
        "records",
        "+0x16, +0x18 hinge pops",
        ADJUSTED,
        "damage._tree_fields",
        "hinge transforms popped where the subtree ends",
        ["+0x0C bit 0, +0x16, +0x18 = _tree_fields"],
        ["record +0x16/+0x18 hinge pops"],
    ),
    (
        "records",
        "+0x1A marker index",
        ADJUSTED,
        "exhaust insert/remove",
        "into the marker table",
        ["marker indices: 0..n-1 once each, in record order"],
        ["record +0x1A marker index"],
    ),
    (
        "records",
        "+0x1C hinge, +0x1E body, +0x27 mirror, +0x3E LOD-link, +0x40 attachment, +0x42 wheel index",
        COPIED,
        "",
        "table indices",
        [
            "hinge indices: 0..n-1 once each, in record order",
            "body indices: 0..n-1 once each, in record order",
            "mirror indices: 0..n-1 once each, in record order",
            "lod_link indices: 0..n-1 once each, in record order",
            "attachment indices: 0..n-1 once each, in record order",
            "wheel indices: 0..n-1 once each, in record order",
        ],
        [
            "record +0x1C hinge index",
            "record +0x1E body index",
            "record +0x27 mirror index",
            "record +0x3E LOD-link index",
            "record +0x40 attachment index",
            "record +0x42 wheel index",
        ],
    ),
    (
        "records",
        "+0x26 seat (gauge) index",
        ADJUSTED,
        "add_gauge_records",
        "into the gauge table",
        ["seat indices: 0..n-1 once each, in record order"],
        ["record +0x26 seat index"],
    ),
    (
        "records",
        "+0x30 light index",
        COPIED,
        "",
        "into the light table",
        ["light indices: 0..n-1 once each, in record order", "lights: one type-17 record per table entry"],
        ["record +0x30 light index"],
    ),
    (
        "records",
        "+0x20 collision offset",
        COPIED,
        "",
        "PDL model",
        [
            "collision offsets fall in record order, root's model last in the PDL",
            "PDL model count = records with a collision model",
        ],
        ["record +0x20 collision offset"],
    ),
    (
        "records",
        "+0x2A panel-data index",
        COPIED,
        "",
        "s9 byte 1 of every vertex (damage.md)",
        [
            "+0x2A over part records: from 0 in record order, steps of 0 or 1 (shared indices)",
            "+0x2A on wheel records = 0",
        ],
        ["record +0x2A panel-data index"],
    ),
    (
        "records",
        "+0x34 transform slot",
        COPIED,
        "",
        "s9 byte 0 of every vertex (damage.md)",
        ["+0x34 over part records: 0.. in record order (some parts have none)"],
        ["record +0x34 transform slot"],
    ),
    ("records", "+0x2C", BLIND, "", "1 or 3; 0 or 4", [], ["record +0x2C"]),
    (
        "records",
        "+0x24, +0x28, +0x2E, +0x32..0x3D",
        BLIND,
        "",
        "-1 / 0 fill on 99%+",
        [],
        ["record +0x24, +0x28, +0x2E, +0x32..0x3D (-1/0 fill)"],
    ),
    ("records", "+0x44..0x4F", BLIND, "", "exporter leftovers (1, 4, a pointer)", [], ["record +0x44 junk"]),
    # hinges
    (
        "hinges",
        "pivot: wheels, hub knuckles (155/156)",
        ADJUSTED,
        "forza2dsf.patch_hubs, settle_wheels",
        "moved with the hub",
        [],
        ["hinges pivot [wheel]", "hinges pivot [suspension]"],
    ),
    ("hinges", "pivot: struts (188/190)", COPIED, "", "axle pivot, not moved", [], []),
    (
        "hinges",
        "pivot: steering column",
        GENERATED,
        "cockpit.patch_hierarchy",
        "Forza wheel hub",
        [],
        ["hinges pivot [steering]"],
    ),
    (
        "hinges",
        "pivot: gauge cluster (185)",
        ADJUSTED,
        "cockpit.patch_hierarchy",
        "mean of the moved gauges",
        [],
        ["hinges pivot [cluster]"],
    ),
    (
        "hinges",
        "pivot: panels (doors, bonnet, boot, bumpers ...)",
        ADJUSTED,
        "DamagePlan.patch_upvh",
        "host panel box to Forza panel box",
        [],
        ["hinges pivot [panel]"],
    ),
    (
        "hinges",
        "axis: steering column",
        GENERATED,
        "cockpit.patch_hierarchy",
        "Forza column",
        [],
        ["hinges axis [steering]"],
    ),
    (
        "hinges",
        "axis: the rest",
        COPIED,
        "",
        "door verticals, bonnet x ...",
        ["hinge +0x28..0x2F zero, pivot w 1, axis w 0"],
        ["hinges axis [panel]", "hinges axis [wheel]", "hinges axis [suspension]", "hinges axis [cluster]"],
    ),
    (
        "hinges",
        "limit, param",
        BLIND,
        "",
        "per role (bumpers 0.1257 / 4000 ...); limit 0 doors swing anyway",
        [],
        ["hinges limit [panel]", "hinges param [panel]"],
    ),
    # bodies
    (
        "bodies",
        "rotation",
        COPIED,
        "",
        "panel frame (tilted bonnet, boot)",
        [],
        ["bodies rotation [panel]", "bodies rotation [wheel]"],
    ),
    (
        "bodies",
        "position: panels",
        ADJUSTED,
        "DamagePlan.patch_upvh",
        "-(R^T c), c the panel's centre of mass, box-mapped",
        [],
        ["bodies position [panel]"],
    ),
    ("bodies", "position: wheels", ADJUSTED, "patch_hubs", "negated hub", [], ["bodies position [wheel]"]),
    (
        "bodies",
        "inertia: panels",
        ADJUSTED,
        "DamagePlan.patch_upvh",
        "scaled by the box ratio",
        [],
        ["bodies inertia [panel]"],
    ),
    (
        "bodies",
        "inertia: wheels; mass",
        COPIED,
        "",
        "kg (bumpers 12-20, doors 8-9)",
        [],
        ["bodies inertia [wheel]", "bodies mass [panel]", "bodies mass [wheel]"],
    ),
    (
        "bodies",
        "+0x50 4x4, +0x90 zeros",
        BLIND,
        "",
        "identity",
        ["body +0x50 4x4 = identity, +0x90..0xAF zero"],
        ["bodies matrix [panel]"],
    ),
    # markers
    (
        "markers",
        "steering (47) position, quaternion",
        GENERATED,
        "cockpit.patch_hierarchy",
        "wheel, hands, chase driver, head camera x",
        [],
        ["markers position [steering]", "markers quaternion [steering]"],
    ),
    (
        "markers",
        "seats (10, 11) position",
        GENERATED,
        "cockpit.patch_seats",
        "driver model",
        [],
        ["markers position [seat]"],
    ),
    ("markers", "seats (10, 11) quaternion", COPIED, "", "seat tilt", [], ["markers quaternion [seat]"]),
    (
        "markers",
        "exhaust (77) position, quaternion",
        GENERATED,
        "exhaust.patch_upvh",
        "smoke, backfire, exhaust sound",
        [],
        ["markers position [exhaust]", "markers quaternion [exhaust]"],
    ),
    (
        "markers",
        "attachment markers (85, 86)",
        BLIND,
        "",
        "with the attachment table; not decoded",
        ["attachments hang on marker records (type 3)", "attachment markers: roles 85, 86"],
        ["markers position [attach]", "markers quaternion [attach]"],
    ),
    (
        "markers",
        "other markers (92, 63/64, 65/67, 69/70, 71..76, 81, 46, 0 ...)",
        BLIND,
        "",
        "at the host's positions on the Forza body",
        [],
        ["markers position [other]", "markers quaternion [other]"],
    ),
    # gauges
    (
        "gauges",
        "needle pivot, dial quaternion, sweep",
        GENERATED,
        "cockpit.patch_hierarchy",
        "cockpit.md Gauges",
        [],
        ["seats pivot [gauge]", "seats quaternion [gauge]", "seats sweep [gauge]"],
    ),
    (
        "gauges",
        "gauge id, hide flag",
        COPIED,
        "add_gauge_records adds missing ids",
        "1 tachometer, 2 speedometer",
        ["seat (gauge) +0x29..0x2F zero, id 1 or 2"],
        ["seats gauge id [gauge]", "seats hide flag [gauge]"],
    ),
    # mirrors
    (
        "mirrors",
        "position",
        GENERATED,
        "cockpit.patch_hierarchy",
        "Forza cockpit mirror",
        [],
        ["mirrors position [panel]"],
    ),
    # lights
    (
        "lights",
        "position, direction, flags",
        GENERATED,
        "lights.LampPlan.patch_upvh",
        "lights.md light table",
        [],
        [
            "lights position [light]",
            "lights direction [light]",
            "lights flags b3 type [light]",
            "lights flags b2 colour [light]",
            "lights flags b0 variant [light]",
        ],
    ),
    (
        "lights",
        "entry count, ids, type-17 records and their parents",
        COPIED,
        "",
        "a lamp breaks with its record's parent; the host's count limits the lamps",
        ["light ids: a permutation of 0..n-1", "a light's parent is a part record (type 1)"],
        ["lights id [light]"],
    ),
    ("lights", "+0x20 pi, pi", COPIED, "", "cone angles?", ["light +0x20, +0x24 = pi, pi"], ["lights pi pi [light]"]),
    (
        "lights",
        "+0x30 tail",
        BLIND,
        "",
        "exporter leftover, one value per vehicle",
        ["light +0x30..0x3F the same in every entry of the vehicle"],
        ["lights junk [light]"],
    ),
    # LOD links, attachments, wheels
    (
        "LOD links",
        "interior and exterior part UIDs",
        GENERATED,
        "renew_uids, split_rear_wheel",
        "which records draw in the interior and exterior",
        ["records with a LOD link: types 1, 2, 5", "every exterior LOD link resolves to an exterior MODL part"],
        ["lod_links present [wheel]", "lod_links present [chassis]"],
    ),
    (
        "attachments",
        "position, +0x18",
        BLIND,
        "",
        "not decoded; never moved",
        [],
        ["attachments position [attach]", "attachments junk [attach]"],
    ),
    (
        "attachments",
        "+0x10 exterior part UID",
        GENERATED,
        "renew_uids",
        "",
        ["attachment +0x10 = an exterior MODL part UID"],
        ["attachments part uid [attach]"],
    ),
    (
        "wheels",
        "hub position",
        GENERATED,
        "patch_hubs, settle_wheels",
        "Forza wheelbase, track, ride height",
        [],
        ["wheels hub [wheel]"],
    ),
    (
        "wheels",
        "quaternion",
        COPIED,
        "",
        "left/right turn, camber",
        ["wheel quaternion (0,1,0,0) at -x, identity at +x"],
        ["wheels quaternion [wheel]"],
    ),
    # PDL
    ("PDL", "header", COPIED, "", "", ["PDL header {1, model count, PDL size - 0x10, 0x10}"], []),
    (
        "PDL",
        "root hull",
        ADJUSTED,
        "DamagePlan.patch_upvh (hull)",
        "host body box to Forza body box",
        [],
        ["PDL model [root]"],
    ),
    ("PDL", "panel boxes", ADJUSTED, "DamagePlan.patch_upvh", "scaled 0.67-1.5x", [], ["PDL model [panel]"]),
    (
        "PDL",
        "wheel cylinders",
        ADJUSTED,
        "forza2dsf.fit_wheel_collision",
        "radius, width from the Forza tyres",
        [],
        ["PDL model [wheel]"],
    ),
    ("PDL", "primitive flags (0x900/0x901) and other words", BLIND, "", "", [], []),
    # other chunks
    ("VO3D", "+0 spool id", GENERATED, "clone_vehicle.patch_ids", "", ["VO3D +0 spool id = hierarchy id"], []),
    (
        "VSRT",
        "engine sound",
        GENERATED,
        "engine_sound",
        "Forza loops in a vanilla template bank (host's on failure)",
        [],
        [],
    ),
    ("SPSR", "paint sets", GENERATED, "paint.install", "Forza factory colours, closest vanilla shading", [], []),
    ("MODL", "LOD tables, submodels", GENERATED, "rebuild_modl", "", [], []),
    (
        "MODL",
        "part records: which parts, their boxes",
        COPIED,
        "",
        "the host's oriented boxes; runtime use untraced",
        [],
        [],
    ),
    ("DRSP", "materials (MACO)", ADJUSTED, "dsf_materials", "host materials, Forza atlas", [], []),
    ("SSIC", "VSCT category byte", COPIED, "", "vehicle class (hosts.md)", [], []),
    ("SSIC", "VSCT other bytes", BLIND, "", "", [], []),
    (
        "Lua",
        "vehicleStats name, badge, stats; stat bars",
        ADJUSTED,
        "register.lua, handling_dsf.stat_bars",
        "bars generated, the rest the host's",
        [],
        [],
    ),
]

# VO3D parameters written from the Forza car (handling_dsf rules Z, A, TP, CY, CZ; the
# cockpit, cameras and gauges); every other parameter a port changes is the host's (or a
# donor vanilla car's) value moved by a fleet rule.
VO3D_GENERATED = {0, 15, 16, 17, 18, 30, 32, 42, 160, 298, 360, 366, 367, 516, 392}
VO3D_GENERATED |= {87, 95, 430, 431, 432, 535, 536, 537, 538, 559, 605, 606}


# Unnamed in handling.PARAMS but decoded in a research note: p525 (handling.md, surface
# grip), p596/p597 (engine audio rpm and throttle), p469 (wheels.md, the rest offset).
VO3D_UNDERSTOOD = {469, 525, 596, 597}


def vo3d_class(pid: int, c: dict) -> str:
    moved = c.get("changed", 0) + c.get("added", 0) + c.get("removed", 0)
    if moved:
        return GENERATED if pid in VO3D_GENERATED else ADJUSTED
    name = H.PARAMS[pid].name if pid in H.PARAMS else ""
    named = name and not (name[0] == "p" and name[1:].isdigit())
    return COPIED if named or pid in VO3D_UNDERSTOOD else BLIND


def inventory(rules: dict | None, diff: dict | None) -> dict:
    by_rule = {r["rule"]: r for r in (rules or {}).get("rules", [])}
    rows, counts = [], Counter()
    for group, field, cls, by, meaning, rule_names, keys in FIELDS:
        holds = [f"{by_rule[n]['holds']}/{by_rule[n]['of']} {by_rule[n]['unit']}" for n in rule_names if n in by_rule]
        changed = ""
        if diff and keys:
            # a key names a field, or a record field over every record class ("record +0x0E [...]")
            e = [v for k, v in diff["upvh"].items() if any(k == x or k.startswith(x + " [") for x in keys)]
            changed = f"{sum(x[2] for x in e)}/{sum(x[3] for x in e)}"
        rows.append([group, field, cls, by, meaning, "; ".join(holds), changed])
        counts[((group in ("VO3D", "VSRT", "SPSR", "MODL", "DRSP", "SSIC", "Lua") and "other") or "UPVH", cls)] += 1
    vo3d = []
    if diff:
        for pid, c in diff["vo3d"].items():
            cls = vo3d_class(int(pid), c)
            moved = c.get("changed", 0) + c.get("added", 0) + c.get("removed", 0)
            vo3d.append([int(pid), H.name(int(pid)), cls, moved, c["cars"]])
            counts[("VO3D params", cls)] += 1
    return {"rows": rows, "vo3d": vo3d, "counts": {f"{a}: {b}": n for (a, b), n in sorted(counts.items())}}


def print_inventory(out: dict) -> None:
    print("| Chunk | Field | Class | Written by | Meaning, evidence | Vanilla rule holds | Ports changed (entries) |")
    print("| --- | --- | --- | --- | --- | --- | --- |")
    for row in out["rows"]:
        print("| " + " | ".join(x.replace("|", "/") for x in row) + " |")
    print()
    for key, n in out["counts"].items():
        print(f"{key}: {n}")
    for cls in (GENERATED, ADJUSTED, COPIED, BLIND):
        pids = [f"p{p}" for p, _, c, _, _ in out["vo3d"] if c == cls]
        print(f"\nVO3D {cls} ({len(pids)}): {' '.join(pids)}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("rules")
    r.add_argument("--archive", type=Path, default=VANILLA)
    r.add_argument("--json", type=Path)
    d = sub.add_parser("diff")
    d.add_argument("--build", type=Path, required=True, help="folder with batch-report.json and dngvehicles.sp")
    d.add_argument("--vanilla", type=Path, default=VANILLA)
    d.add_argument("--limit", type=int, help="only the first N cars of the report")
    d.add_argument("--json", type=Path)
    i = sub.add_parser("inventory")
    i.add_argument("--rules", type=Path, help="rules --json output")
    i.add_argument("--diff", type=Path, help="diff --json output")
    i.add_argument("--json", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "rules":
        out = vanilla_rules(args.archive)
        print_rules(out)
    elif args.cmd == "diff":
        out = build_diff(args.build, args.vanilla, args.limit)
        print_diff(out)
    else:
        load = lambda p: json.loads(p.read_text()) if p else None  # noqa: E731
        out = inventory(load(args.rules), load(args.diff))
        print_inventory(out)
    if args.json:
        args.json.write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main(sys.argv[1:])
