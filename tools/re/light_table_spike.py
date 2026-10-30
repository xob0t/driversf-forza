"""Spike (research/host-free.md, stage 1): a ported car's light table generated from its
Forza lamps instead of the host's entries. Throwaway: it wraps port_car at run time and
changes no converter code.

What it does differently from the converter (convert/lights.py keeps the host's table:
its size, ids and type-17 records, and lamps share entries when the host has too few):

- every lamp the plan lights gets an entry of its own (light id = its rank in the lamp
  plan's priority order, at most MAX_LAMPS), so no lamp shares one and none stays unlit
  for want of an entry;
- after the conversion, the host's type-17 records and light table are dropped and new
  ones written: one record per lamp, a leaf placed as the first child of its parent
  (every vanilla parent lists its lights first), the table in record order (as in all
  150 vanilla hierarchies), id at +0x28 the lamp's light id, pi, pi and the host's
  constant tail;
- the parent comes from roles, not from the host's light records: front lamps under the
  head lamp part of their side (role 50 at +x, 51 at -x; centre: front structure 37,
  front bumper 35), rear lamps under the chassis part (role 38), a centre stop lamp under
  the boot lid (role 5) when there is one: the vanilla majorities (`host_free.py rules`,
  light_parents);
- the header's record and light counts, every ancestor's subtree size and the tree fields
  (+0x0C bit 0, +0x16, +0x18; damage._tree_fields) are written again.

    uv run python tools/re/light_table_spike.py build --out out/spike/after/dngvehicles.sp \
        --car SHE_Series1_99 --car MAZ_Miata_94 [--first-id 1] [--normal]
    uv run python tools/re/light_table_spike.py show out/spike/after/dngvehicles.sp --id 1

--normal builds the same cars with the converter as it is (the "before").
"""

from __future__ import annotations

import argparse
import math
import shlex
import struct
import sys
from pathlib import Path

from dsfforza import REPO
from dsfforza.convert import forza2dsf, lights
from dsfforza.convert.damage import _tree_fields
from dsfforza.formats import upvh as U
from dsfforza.formats.vehicle_mesh import child, payload

MAX_LAMPS = 27  # the vanilla maximum; the engine holds 32 lamp objects and 32 shader colours (id + 1)
LIGHT_TYPE = 17
STATE: dict = {}


# --- the lamp plan: one entry per lamp --------------------------------------------------


def _order(plan) -> list:
    drawn = set(plan.tri_lamp.values())
    return sorted(
        (l for k, l in plan.lamps.items() if l.area and k in drawn),
        key=lambda l: (lights.PRIORITY.get(l.function, 9) + 10 * min(l.cluster, 1), not l.front, l.side),
    )


def assign_own_entries(plan, upvh: bytes):
    """LampPlan.assign with an entry per lamp: the converter's assign first (it also ranks
    the lamps), then light ids 0..n-1 in that order and the day-time head lamp rule again
    with every lamp entered."""
    ORIGINAL_ASSIGN(plan, upvh)
    order = _order(plan)
    for lamp in plan.lamps.values():
        lamp.light_id = None
    for i, lamp in enumerate(order[:MAX_LAMPS]):
        lamp.light_id = i
    plan.unlit = [l.key for l in order[MAX_LAMPS:]]
    plan.slots = [(None, l.light_id, l) for l in order[:MAX_LAMPS]]

    def area(functions, side):
        return sum(l.area for l in order if l.front and l.side == side and l.function in functions)

    for lamp in order:
        if lamp.function == lights.HEAD and lamp.front:
            lamp.daytime = area((lights.POSITION, lights.DRL_IND), lamp.side) < lights.DAYTIME_SHARE * area(
                (lights.HEAD,), lamp.side
            )
    STATE["plan"] = plan
    return plan


def keep_host_table(_plan, upvh: bytes) -> bytes:
    """LampPlan.patch_upvh during the conversion: the table is rebuilt after it."""
    return upvh


# --- the hierarchy: new type-17 records and light table ----------------------------------


class Node:
    def __init__(self, raw: bytes):
        self.raw = bytearray(raw)
        self.kids: list[Node] = []

    @property
    def type(self) -> int:
        return struct.unpack_from("<H", self.raw, 0x08)[0]

    @property
    def role(self) -> int:
        return struct.unpack_from("<H", self.raw, 0x0A)[0]


def tree(h) -> Node:
    nodes = [Node(r.raw) for r in h.records]
    stack: list[tuple[int, int]] = []
    for r in h.records:
        while stack and stack[-1][1] <= r.index:
            stack.pop()
        if stack:
            nodes[stack[-1][0]].kids.append(nodes[r.index])
        stack.append((r.index, r.index + r.subtree))
    return nodes[0]


def flatten(node: Node) -> list[Node]:
    out = [node]
    for k in node.kids:
        out += flatten(k)
    struct.pack_into("<H", node.raw, 0x14, len(out))
    return out


def walk(node: Node):
    yield node
    for k in node.kids:
        yield from walk(k)


def parent_for(lamp, parts: dict[int, Node]) -> Node:
    """The record a lamp hangs under (it breaks when that part is damaged)."""
    if lamp.front:
        side = {"L": 50, "R": 51}.get(lamp.side)
        roles = ([side] if side else []) + [37, 35, 38]
    elif lamp.function == lights.BRAKE and lamp.side == "C":
        roles = [5, 38]
    else:
        roles = [38]
    return next(parts[r] for r in roles if r in parts)


def rebuild_light_table(data: bytes, lamps: list) -> tuple[bytes, list]:
    """The UPVH with the host's type-17 records and light table replaced by one record and
    entry per lamp (lamp.light_id set). Returns (UPVH, [(table index, id, parent role,
    lamp key)])."""
    h = U.parse(data)
    host_lights = [r for r in h.records if r.type == LIGHT_TYPE]
    if not host_lights:
        raise ValueError("the host has no light record to copy the record layout from")
    template = bytearray(host_lights[0].raw)
    tail = h.table("lights")[0][0x30:0x40]
    root = tree(h)
    for node in walk(root):
        node.kids = [k for k in node.kids if k.type != LIGHT_TYPE]
    parts: dict[int, Node] = {}
    for node in walk(root):
        if node.type == 1:
            parts.setdefault(node.role, node)
    lamps = sorted(lamps, key=lambda l: l.light_id)
    by_parent: dict[int, list] = {}
    for lamp in lamps:
        by_parent.setdefault(id(parent_for(lamp, parts)), []).append(lamp)
    owner = {}
    for node in walk(root):
        mine = by_parent.get(id(node), [])
        new = []
        for lamp in mine:
            rec = Node(template)
            owner[id(rec)] = (lamp, node.role)
            new.append(rec)
        node.kids = new + node.kids
    records = flatten(root)
    table, report = [], []
    for rec in records:
        if rec.type != LIGHT_TYPE:
            continue
        lamp, role = owner[id(rec)]
        struct.pack_into("<h", rec.raw, 0x30, len(table))
        entry = bytearray(0x40)
        struct.pack_into("<4f", entry, 0x00, *lamp.position, 1.0)
        struct.pack_into("<4f", entry, 0x10, *lamp.direction, 0.0)
        struct.pack_into("<2fII", entry, 0x20, math.pi, math.pi, lamp.light_id, lamp.flags)
        entry[0x30:0x40] = tail
        report.append((len(table), lamp.light_id, role, lamp.key, f"{lamp.flags:#010x}"))
        table.append(bytes(entry))
    raws = [r.raw for r in records]
    for r, fields in zip(raws, _tree_fields(raws), strict=True):
        struct.pack_into("<H", r, 0x0C, (struct.unpack_from("<H", r, 0x0C)[0] & ~1) | fields[0])
        struct.pack_into("<HH", r, 0x16, *fields[1:])
    start = h.offsets["lights"]
    end = start + h.counts["lights"] * 0x40
    tables_start = U.HEADER_SIZE + len(h.records) * U.RECORD_SIZE
    out = bytearray(data[: U.HEADER_SIZE])
    out += b"".join(bytes(r) for r in raws)
    out += data[tables_start:start] + b"".join(table) + data[end:]
    struct.pack_into("<I", out, 0x10, len(raws))
    struct.pack_into("<H", out, 0x32, len(table))
    U.parse(bytes(out))  # the layout still parses: tables end where the PDL starts
    return bytes(out), report


def convert_then_rebuild(stream, full, exterior, car, dims, opts, log=print, interior=None):
    STATE.pop("plan", None)
    report = ORIGINAL_CONVERT(stream, full, exterior, car, dims, opts, log=log, interior=interior)
    plan = STATE.get("plan")
    if plan is not None and not opts.skip_full:
        upvh = child(child(full, "NONR"), "UPVH")
        lit = [l for l in plan.lamps.values() if l.light_id is not None]
        upvh.payload, table = rebuild_light_table(payload(stream, upvh), lit)
        log(f"spike: light table generated, {len(table)} entries (one per lamp)")
        for row in table:
            log(f"  spike light {row[0]:2d}: id {row[1]:2d} under role {row[2]:3d}, {row[3]} {row[4]}")
        report["spike_lights"] = [list(map(str, row)) for row in table]
    return report


ORIGINAL_ASSIGN = lights.LampPlan.assign
ORIGINAL_CONVERT = forza2dsf.convert


def cmd_build(args) -> None:
    from dsfforza.build import port_batch, port_car

    if not args.normal:
        lights.LampPlan.assign = assign_own_entries
        lights.LampPlan.patch_upvh = keep_host_table
        forza2dsf.convert = convert_then_rebuild
    args.out.parent.mkdir(parents=True, exist_ok=True)
    argv = ["--dsf", str(args.dsf), "--out", str(args.out), "--first-id", str(args.first_id)]
    for car in args.car:
        argv += ["--car", car]
    port_car.main(argv + shlex.split(port_batch.DEFAULT_PORT_OPTIONS) + (args.port or []))


def cmd_show(args) -> None:
    from dsfforza.formats.chnk import read_chunk

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        full = U.entries_for(stream, root.entries, args.id, None)[0]
        h = U.parse(payload(stream, child(child(full, "NONR"), "UPVH")))
    par: dict[int, int] = {}
    stack: list[tuple[int, int]] = []
    for r in h.records:
        while stack and stack[-1][1] <= r.index:
            stack.pop()
        if stack:
            par[r.index] = stack[-1][0]
        stack.append((r.index, r.index + r.subtree))
    table = h.table("lights")
    print(f"{len(h.records)} records, {len(table)} lights")
    for r in h.records:
        if r.type == LIGHT_TYPE:
            e = table[r.light]
            pos = struct.unpack_from("<3f", e)
            lid, flags = struct.unpack_from("<II", e, 0x28)
            p = h.records[par[r.index]]
            print(
                f"  record {r.index:3d} light {r.light:2d} id {lid:2d} flags {flags:#010x} "
                f"under record {p.index} role {p.role}: ({pos[0]:+.2f}, {pos[1]:+.2f}, {pos[2]:+.2f})"
            )


def cmd_check(args) -> None:
    """Every light byte (s9 byte 2) the full and exterior entries' vertices carry against
    the light table: an entry with that id exists, and the vertices' centroid is near it."""
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk

    with args.archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = U.entries_for(stream, root.entries, args.id, None)
        h = U.parse(payload(stream, child(child(kinds[0], "NONR"), "UPVH")))
    entries = {struct.unpack_from("<I", e, 0x28)[0]: struct.unpack_from("<3f", e) for e in h.table("lights")}
    for kind in (0, 2):
        _, mesh = vm.load(args.archive, root.entries.index(kinds[kind]), root.entries)
        sums: dict[int, list] = {}
        for sub in mesh.submodels:
            vb = mesh.vertex_buffers[sub.vertex_buffer]
            s9 = next((e for e in vb.elements if e.semantic == 9), None)
            if s9 is None:
                continue
            for _, tri in vm.triangles(mesh, sub):
                for v in tri:
                    b = vb.read(s9, v)[2]
                    if b:
                        acc = sums.setdefault(b, [0, 0.0, 0.0, 0.0])
                        p = vm.position(vb, v)
                        acc[0] += 1
                        for k in range(3):
                            acc[k + 1] += p[k]
        worst = []
        for b, (n, *s) in sorted(sums.items()):
            c = tuple(x / n for x in s)
            e = entries.get(b - 1)
            worst.append((round(math.dist(c, e), 3) if e else None, b - 1))
        missing = [lid for d, lid in worst if d is None]
        far = sorted((d, lid) for d, lid in worst if d is not None)[-3:]
        print(
            f"kind {kind}: {len(sums)} light ids on vertices, {len(entries)} table entries; "
            f"ids without an entry {missing}; farthest centroid-entry distances {far}"
        )


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--car", action="append", required=True)
    b.add_argument("--dsf", type=Path, default=REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp")
    b.add_argument("--first-id", type=int, default=1)
    b.add_argument("--normal", action="store_true", help="the converter as it is (the before build)")
    b.add_argument("--port", action="append", help="extra port_car options")
    s = sub.add_parser("show")
    s.add_argument("archive", type=Path)
    s.add_argument("--id", type=int, required=True)
    c = sub.add_parser("check")
    c.add_argument("archive", type=Path)
    c.add_argument("--id", type=int, required=True)
    args = ap.parse_args(argv)
    {"build": cmd_build, "show": cmd_show, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    main(sys.argv[1:])
