"""Copy the out/cache files one native car build read (its work/<media>.fp.json) into a
fresh folder, for the Pyodide prototype (research/web-builder.md, "Prototype").

    uv run python tools/web-builder/seed_cache.py out/native/work/VW_Beetle_04.fp.json \\
        --cache out/cache --to out/web-builder/cache-warm [--only-wav]

--only-wav copies just the WAVs vgmstream decoded (cache/sound/<game>/<bank>/*.wav): the
one step of the conversion that runs a native program. Everything else the car needs is
then made again in Pyodide from the discs and the vanilla archive ("cold" run).
The fingerprint keeps paths lower-cased; each is copied under its real name. A decoded
bank's folder is copied whole: the car may read only some of its WAVs, and
engine_sound._decoded takes the folder only with one WAV per subsong.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


def real_case(path: Path) -> Path:
    """`path` with every component as the folder lists it (Windows ignores case)."""
    out = Path(path.anchor)
    for part in path.parts[1:]:
        names = {p.name.lower(): p.name for p in out.iterdir()}
        out = out / names.get(part.lower(), part)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("fp", type=Path)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--to", type=Path, required=True)
    parser.add_argument("--only-wav", action="store_true")
    args = parser.parse_args()
    cache = real_case(args.cache.resolve())
    files = json.loads(args.fp.read_text(encoding="utf-8"))["files"]
    copied = size = 0
    for name in files:
        path = Path(name)
        try:
            rel = path.relative_to(cache)
        except ValueError:
            try:
                rel = real_case(path).relative_to(cache)
            except (ValueError, OSError):
                continue
        if args.only_wav and not (rel.parts[0].lower() == "sound" and rel.suffix.lower() == ".wav"):
            continue
        source = real_case(cache / rel)
        sources = sorted(source.parent.glob("*.wav")) if source.suffix.lower() == ".wav" else [source]
        for one in sources:
            target = args.to / one.relative_to(cache)
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(one, target)
            copied += 1
            size += one.stat().st_size
    print(f"{copied} files, {size / 2**20:.1f} MiB -> {args.to}")


if __name__ == "__main__":
    main()
