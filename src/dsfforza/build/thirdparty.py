"""Third-party tools the build and the install need, downloaded on first use from their
upstream releases, each pinned to one version and SHA-256, and cached in out/tools-dl/.
Nothing of these is in the repo or the release zip.

    uv run dsff thirdparty list             # what is pinned, and what is cached
    uv run dsff thirdparty fetch [NAME ...] # download (and check) them now

- vgmstream (decodes Forza's XMA engine sound; src/dsfforza/formats/forza_audio.py looks
  for out/tools-dl/vgmstream/vgmstream-cli.exe): unpacked there.
- DriverNGHook 0.7.2 (runs the Lua that registers the cars) and the Ultimate ASI Loader
  (loads DriverNGHook and dsfforza.asi): tools/install.ps1 copies them into the game
  folder (src/dsfforza/build/install.py passes them as -HookArchive and -AsiLoader).
  DriverNGHook's .7z is unpacked here (src/dsfforza/formats/sevenzip.py), not by
  install.ps1's tar.exe: Windows builds whose tar has no liblzma stopped the install with
  "tar.exe: LZMA codec is unsupported".

A copy that is already there with the pinned hash (out/tools-dl, or a developer's
_ref/downloads) is used without a download.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import ssl
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from dsfforza import REPO

TOOLS_DL = REPO / "out" / "tools-dl"


@dataclass(frozen=True)
class Tool:
    title: str
    version: str
    url: str
    sha256: str
    folder: str  # under out/tools-dl
    unpack: str = ""  # a file the unpacked zip or 7z must hold (empty: keep the archive as it is)
    also: tuple[str, ...] = ()  # other places a copy may already be (relative to the repo)

    @property
    def file(self) -> str:
        return self.url.rsplit("/", 1)[1]


TOOLS = {
    "vgmstream": Tool(
        "vgmstream",
        "r2117",
        "https://github.com/vgmstream/vgmstream/releases/download/r2117/vgmstream-win64.zip",
        "6C4A8A3813864FEFED081BBD337DBC0AD93BF88E0B92F5DB98D7AB258B22DC6C",
        "vgmstream",
        unpack="vgmstream-cli.exe",
        also=("out/tools-dl/vgmstream/vgmstream.zip",),
    ),
    "dnghook": Tool(
        "DriverNGHook",
        "0.7.2",
        "https://github.com/ReHamster/DriverNGToolkit/releases/download/0.7.2/DNGHook_072.7z",
        "C2AE54B0A7F0FD2D00CBCFE9ED721017F8BA84D1FEC618D53FC99F4C78BC3056",
        "dnghook-0.7.2",
        unpack="plugins/DriverNGHook.asi",
        also=("_ref/downloads/dnghook-0.7.2/DNGHook_072.7z",),
    ),
    "asi-loader": Tool(
        "Ultimate ASI Loader (Win32)",
        "v9.7.4",
        "https://github.com/ThirteenAG/Ultimate-ASI-Loader/releases/download/v9.7.4/Ultimate-ASI-Loader-NoPDB.zip",
        "14B3A1AD018899571AC9AA01482977F3C6D49E6CBA99F552D01C5ACACD1315E1",
        "ultimate-asi-loader-v9.7.4",
    ),
}


class DownloadError(Exception):
    pass


def open_url(url: str):
    """urlopen with the operating system's certificate check (truststore). Python's own check
    only trusts the root certificates already in the Windows store, and Windows fetches most
    roots the first time something of its own needs them: on a fresh Windows the download from
    GitHub's release host (release-assets.githubusercontent.com) failed with
    CERTIFICATE_VERIFY_FAILED while github.com itself passed."""
    import truststore

    return urllib.request.urlopen(url, context=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=120)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest().upper()


def archive_path(tool: Tool, root: Path = TOOLS_DL) -> Path:
    return Path(root) / tool.folder / tool.file


def cached(tool: Tool, root: Path = TOOLS_DL, repo: Path = REPO) -> Path | None:
    """A copy with the pinned hash that is already on disk, or None."""
    for path in (archive_path(tool, root), *(Path(repo) / a for a in tool.also)):
        if path.is_file() and file_sha256(path) == tool.sha256:
            return path
    return None


def ready(name: str, root: Path = TOOLS_DL, repo: Path = REPO) -> bool:
    """The tool is usable without a download."""
    tool = TOOLS[name]
    if tool.unpack and (Path(root) / tool.folder / tool.unpack).is_file():
        return True
    return cached(tool, root, repo) is not None


def download(url: str, target: Path, sha256: str, opener=open_url) -> None:
    """url to target, through target.part; raises DownloadError when the bytes don't hash to sha256."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    digest = hashlib.sha256()
    try:
        with opener(url) as response, part.open("wb") as out:
            while block := response.read(1 << 20):
                digest.update(block)
                out.write(block)
    except OSError as error:
        part.unlink(missing_ok=True)
        raise DownloadError(f"could not download {url}: {error}") from error
    if digest.hexdigest().upper() != sha256.upper():
        part.unlink(missing_ok=True)
        raise DownloadError(f"{url}: SHA-256 {digest.hexdigest().upper()}, expected {sha256.upper()}; not used")
    part.replace(target)


def fetch(name: str, root: Path = TOOLS_DL, repo: Path = REPO, opener=open_url, log=print) -> Path:
    """The tool's archive (or, for an unpacked tool, its folder), downloading it if needed."""
    tool = TOOLS[name]
    folder = Path(root) / tool.folder
    if tool.unpack and (folder / tool.unpack).is_file():
        return folder
    path = cached(tool, root, repo)
    if path is None:
        path = archive_path(tool, root)
        log(f"downloading {tool.title} {tool.version} from {tool.url}")
        download(tool.url, path, tool.sha256, opener)
    if not tool.unpack:
        return path
    unpack(path, folder)
    if not (folder / tool.unpack).is_file():
        raise DownloadError(f"{path} has no {tool.unpack}")
    (folder / "pinned.json").write_text(
        json.dumps({"version": tool.version, "url": tool.url, "sha256": tool.sha256}, indent=1) + "\n",
        encoding="utf-8",
    )
    return folder


def unpack(archive: Path, folder: Path) -> Path:
    """A zip or 7z into folder (returned)."""
    if Path(archive).suffix.lower() == ".7z":
        unpack_7z(archive, folder)
    else:
        unpack_zip(archive, folder)
    return folder


def unpack_7z(archive: Path, folder: Path) -> None:
    """Every member of a 7z into folder (members that would land outside it are refused)."""
    from dsfforza.formats import sevenzip

    try:
        sevenzip.SevenZip(archive).extract(folder)
    except (sevenzip.SevenZipError, OSError) as error:
        raise DownloadError(f"could not unpack {archive}: {error}") from error


def unpack_zip(archive: Path, folder: Path) -> None:
    """Every member of a zip into folder (members that would land outside it are refused)."""
    folder.mkdir(parents=True, exist_ok=True)
    base = folder.resolve()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            target = (folder / member.filename).resolve()
            if base != target and base not in target.parents:
                raise DownloadError(f"{archive}: {member.filename} points outside the folder")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="the pinned tools and whether they are cached")
    f = sub.add_parser("fetch", help="download and check the tools now")
    f.add_argument("names", nargs="*", metavar="NAME", help=", ".join(TOOLS))
    args = parser.parse_args(argv)
    unknown = [n for n in getattr(args, "names", []) if n not in TOOLS]
    if unknown:
        parser.error(f"unknown tool {', '.join(unknown)} (known: {', '.join(TOOLS)})")
    if args.cmd == "list":
        for name, tool in TOOLS.items():
            state = "cached" if ready(name) else "not downloaded"
            print(f"{name:11} {tool.title} {tool.version}: {state}\n            {tool.url}\n            {tool.sha256}")
        return 0
    for name in args.names or list(TOOLS):
        try:
            print(f"{name}: {fetch(name)}")
        except DownloadError as error:
            print(f"{name}: {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
