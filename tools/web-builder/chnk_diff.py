"""Which leaves of two CHNK files differ (research/web-builder.md, "Byte identity").

    uv run python tools/web-builder/chnk_diff.py a.chnk b.chnk

Walks both trees in order and prints each leaf whose path, size or bytes differ, with the
first differing offset.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from dsfforza.formats.chnk import read_chunk, walk


def leaves(path: Path) -> list[tuple[str, int, bytes]]:
    out = []
    with path.open("rb") as stream:
        root = read_chunk(stream, 0)
        names: dict[tuple[int, ...], str] = {}
        for where, entry in walk(root.entries):
            names[where] = f"{entry.tag}:{entry.name or ''}"
            if entry.is_chunk:
                continue
            stream.seek(entry.offset)
            data = stream.read(entry.size)
            label = "/".join(names[where[: i + 1]] for i in range(len(where)))
            out.append((label, entry.size, data))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    args = parser.parse_args()
    a, b = leaves(args.a), leaves(args.b)
    print(f"{len(a)} and {len(b)} leaves")
    same = 0
    for (pa, sa, da), (pb, sb, db) in zip(a, b):
        if pa == pb and da == db:
            same += 1
            continue
        first = next((i for i, (x, y) in enumerate(zip(da, db)) if x != y), min(sa, sb))
        print(f"{pa} {sa:#x} | {pb} {sb:#x}: first difference at {first:#x}", hashlib.sha256(da).hexdigest()[:8])
    print(f"{same} identical")


if __name__ == "__main__":
    main()
