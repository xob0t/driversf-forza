"""The release zip: the code, the prebuilt dsfforza.asi and the install.cmd launcher.

    uv run dsff package --asi out/asi/dsfforza.asi --version v1.2.0 --out dist

.github/workflows/release.yml runs this after building the plugin. The zip holds the
git-tracked files a player's install needs (the Python package, pyproject.toml and
uv.lock, tools/install.ps1, the registration Lua, the car lists, the launcher) plus:

    plugin/dsfforza.asi, plugin/dsfforza.ini, plugin/dsfforza.build.json
    assets/PUT-YOUR-FORZA-ISOS-HERE.txt

Nothing from the game or Forza, and no third-party binaries: DriverNGHook, the ASI
loader and vgmstream are downloaded at install time, pinned by version and SHA-256
(src/dsfforza/build/thirdparty.py). `uv run dsff install` in the unzipped folder uses
plugin/dsfforza.asi (src/dsfforza/build/install.py).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from dsfforza import REPO

FILES = (
    "install.cmd",
    "README.md",
    "INSTALL.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
    "tools/install.ps1",
    "tools/asi/dsfforza.ini",
    "tools/game/lua/player/bootstrap.lua",
    "tools/game/lua/dsfforza/register.lua",
)
FOLDERS = ("src/dsfforza/", "tools/batches/", "tools/release/")
# Never in a release, whatever is tracked: game and Forza data, captures, binaries.
REFUSED = (".iso", ".sp", ".fchunk", ".slt", ".xex", ".dat", ".exe", ".dll", ".7z", ".chnk", ".png", ".jpg")
ASSETS_NOTE = """Put your Forza disc images (Xbox 360 .iso files) in this folder:

- Forza Horizon (FH1)
- Forza Motorsport 4, disc 1 and disc 2

The file names don't matter. A car that is only on a disc you don't have is skipped.
Then run install.cmd in the folder above.
"""


def tracked(repo: Path = REPO) -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=repo, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def select(files: list[str]) -> list[str]:
    """The tracked files that go in the zip."""
    keep = [f for f in files if f in FILES or f.startswith(FOLDERS)]
    bad = [f for f in keep if f.lower().endswith(REFUSED)]
    if bad:
        raise SystemExit(f"refusing to package {', '.join(bad)}")
    missing = [f for f in FILES if f not in files]
    if missing:
        raise SystemExit(f"not tracked: {', '.join(missing)}")
    return sorted(keep)


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest().upper()


def commit(repo: Path = REPO) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def build_zip(asi: Path, version: str, out_dir: Path, repo: Path = REPO, files: list[str] | None = None) -> Path:
    from dsfforza.build import install

    asi = Path(asi)
    if not asi.is_file():
        raise SystemExit(f"{asi}: no plugin (tools\\asi\\build.ps1)")
    files = select(tracked(repo) if files is None else files)
    name = f"driversf-forza-{version}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{name}.zip"
    stamp = {
        "version": version,
        "commit": commit(repo),
        "sha256": sha256(asi),
        "sources": install.asi_sources_digest(repo / "tools" / "asi"),
    }
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            z.write(repo / f, f"{name}/{f}")
        z.write(asi, f"{name}/plugin/dsfforza.asi")
        z.write(repo / "tools" / "asi" / "dsfforza.ini", f"{name}/plugin/dsfforza.ini")
        z.writestr(f"{name}/plugin/dsfforza.build.json", json.dumps(stamp, indent=1) + "\n")
        z.writestr(f"{name}/assets/PUT-YOUR-FORZA-ISOS-HERE.txt", ASSETS_NOTE.replace("\n", "\r\n"))
        z.writestr(f"{name}/VERSION", f"{version} {stamp['commit']}\n")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__
    )
    parser.add_argument("--asi", type=Path, default=REPO / "out" / "asi" / "dsfforza.asi", help="the built plugin")
    parser.add_argument("--version", required=True, help="e.g. v1.2.0 (the tag) or a dev label")
    parser.add_argument("--out", type=Path, default=REPO / "dist", help="output folder (default: dist)")
    parser.add_argument("--list", action="store_true", help="print the files that would go in, and stop")
    args = parser.parse_args(argv)
    if args.list:
        print("\n".join(select(tracked())))
        return 0
    target = build_zip(args.asi, args.version, args.out)
    with zipfile.ZipFile(target) as z:
        count = len(z.namelist())
    print(f"wrote {target} ({target.stat().st_size / 2**20:.1f} MiB, {count} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
