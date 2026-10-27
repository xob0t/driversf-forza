"""src/dsfforza/formats/sevenzip.py: the 7z reader dsff install unpacks DriverNGHook with
(Windows' tar.exe without liblzma stopped the install: "tar.exe: LZMA codec is unsupported").

The fixtures are small archives 7-Zip 25 made of synthetic files (FILES below), so the
layout is 7-Zip's own: LZMA1 without an end mark, LZMA as coder 0 with BCJ reading it as
coder 1, LZMA-coded headers. When DNGHook_072.7z is on disk (out/tools-dl, _ref) it is
unpacked as well.

uv run python -m tests sevenzip
"""

from __future__ import annotations

import base64
import struct
import tempfile
import zlib
from pathlib import Path

from dsfforza import REPO
from dsfforza.build import thirdparty
from dsfforza.formats import sevenzip

FILES = {
    "a.txt": b"".join(b"line %d of the fixture\r\n" % i for i in range(60)),
    "dir/code.bin": bytes((0xE8, i, 0x10, 0, 0, 0x90, 0x90)[j] for i in range(120) for j in range(7)),
    "empty.txt": b"",
}
DIRS = ("dir", "emptydir")

# 7z a -mtm=off -mtc=off -mta=off -mf=off <options> <archive> . in a folder holding FILES and DIRS.
FIXTURES = {
    # -m0=LZMA:d=16 (the header LZMA-coded too)
    "lzma": "N3q8ryccAATgfFMynAEAAAAAAAAiAAAAAAAAALjlAoIANhpKHwigJgNNBp34sqWsqUR9Mqv9oFG7huvqWqNe5u/FY0005lH5Cl8y"
    "/H5Dif1aa7RwDqa6snAh/ZhwQGI0PYQ4NOAlTE7rSP36O+QGcSlv5UNx/3SGayxTp/xSS3t+Lc1Pgg0fmBuM3EUEQ+poUVcKFHIX"
    "TRn4vSaFRIvEE3qNjDihEMrqk6hIVMkGPWOYWpZFxGn6oAMrEbIHAq+oF+jhZ2JyreBUk9gPx4V8e207+Tr+CuSYcdEQbCu3Kw5E"
    "DuRIJPREx2Y+oHk5OkqaRWcbeHv8enklkkHVRDMc5lEOFoxCMrFit2mgu2m1sJUojjlFDgU9tvw8TUl9Y4VR8Db27sVWi17pdJZL"
    "VaAJc5LTd/qmj+0K+5CWOD3N97GT7bIAAACBMweuD9U4ngoXJNP+s34x4pabsPJiyqcMUrLvJsOtszd/65xQSsQWkvsXuGmNyWyL"
    "wYX4iXj1KmDhQ8mFbS0EF2JepTDOpq4G2KtBk2vk3DuQkQbGBnbRql3pHTnt1Gh/Uj3SG6Pb/cJut/6LRyYwKV6OigAAFwaBJAEJ"
    "eAAHCwEAASMDAQEFXQAQAAAMgK4KAVI0KUYAAA==",
    # -m0=BCJ -m1=LZMA:d=16
    "bcj_lzma": "N3q8ryccAATdfzFumAEAAAAAAAAiAAAAAAAAAM2/+SoANhpKHwigJgNNBp34sqWsqUR9Mqv9oFG7huvqWqNe5u/FY0005lH5C"
    "l8y/H5Dif1aa7RwDqa6snAh/ZhwQGI0PYQ4NOAlTE7rSP36O+QGcSlv5UNx/3SGayxTp/xSS3t+Lc1Pgg0fmBuM3EUEQ+poUVcK"
    "FHIXTRn4vSaFRIvEE3qNjDihQs1XPAbiR19EmNN0Lzi305Y9hsZ4LHJBpMbCnkqGw+Wb40UZwme+YH9NUraSS49s7V+TF027bxrO"
    "71FcmfpdN1APDpU96C3jXbR+66CERDTOHIpIkrpacUzIty8i0RZ6DQ3CyJb2W7Nm/5A8kMyR5oON4ZnxS1aAb0Yq4Y6XdFIDhFl9"
    "oTmGUV0ZmL9a07SSqCt1t0AAAIEzB64P1ThlT7nGYJcF2V0d8p33X3OGtqQFkCuQ+ryqoNdle0IoYfVXw1kiTqq43K0yKklgbpIw"
    "S9X3rG3HBHjInUvD4zs2rbsCnhy0xsykg9qnmv6d7tqcuIduJH25mIeffFQo9oc+e5Zm7AucLGNq7rMHC5OCEN7unAAXBoEaAQl+"
    "AAcLAQABIwMBAQVdABAAAAyAvgoBFwtjagAA",
    # -m0=Delta:2 -m1=LZMA2:d=16 -mhc=off (a plain header: the names are readable)
    "delta_lzma2": "N3q8ryccAAS9uQre5AEAAAAAAACuAAAAAAAAAM22VdPgCN0B3F0ANho8T7OfhgpC4wsm3eS8ASx5q/z/YnVzL/8/r+p5sLG"
    "S2JJqow7yZoCyC0xEdsUq5jPVQw9DgUmjPpGQoVlngI3D7o9CYHVG/xye9YgU9yedICwr1MFhZ4xVn4sPVq6FX8tdRDtMP+X9aT8+"
    "0voGit/ChQeq4GwRkE0Lb3dLy7zwlfUyrXXNQU7PkwOOt64u69cA9tNc0oEB3cCeoxpflOjWhjIFFezJKKm/+iajRhj8TU0SDMkH"
    "maN08r6sfQUcrJTnXszeY2TiehsnS0b/zkvtPKkjAZ89kVznfpSC4hEDdfY3gGpuAhwjRsTP2aMa0SQt07PNszJXGdsdOCw9NSyA"
    "3DSgBALo7P9S8k1UnYv9xTgLWb1J+9A+C2+RBu46E4MOPkjBHMLSc1E1HbXXD2IX1ITeOKBXejGeCblbnfvXsFhMKD1FdxYWT5Cy"
    "dl6VmmpPWLi/ceNLjnvMalum1TEIEOEiC1nhu7wRCc/vp3MTOK8bazJjD7+Q5viGqBPOxMEnUDVnSRw56LSwpRZn3/Qqb/eSodEe"
    "BOyOu3aiGeaKgZbd64Kq8H4rJcYFIrpxY+Ku6+TrK+1p2Wt3TDLgX/x3sEUyJ7us3VhxSBZ8dvt8GcGrcsj/kZWZ/j8AAQQGAAEJ"
    "geQABwsBAAIhIQEAIQMBAQEADIjeiN4ACA0CCYWWCgHTc4HgHSlMvwAABQUOAeAPASAZAwAAABFVAGQAaQByAAAAZQBtAHAAdAB5"
    "AGQAaQByAAAAZQBtAHAAdAB5AC4AdAB4AHQAAABhAC4AdAB4AHQAAABkAGkAcgAvAGMAbwBkAGUALgBiAGkAbgAAABUWAQAQAAAA"
    "EAAAACAAAAAgAAAAIAAAAAAA",
}


def _archive(folder: Path, name: str, data: bytes | None = None) -> Path:
    path = folder / f"{name}.7z"
    path.write_bytes(base64.b64decode(FIXTURES[name]) if data is None else data)
    return path


def _assert_unpacked(out: Path) -> None:
    for name, data in FILES.items():
        assert (out / name).read_bytes() == data, name
    for name in DIRS:
        assert (out / name).is_dir(), name
    files = {p.relative_to(out).as_posix() for p in out.rglob("*")}
    assert files == set(FILES) | set(DIRS), files


def _rewrite_header(data: bytes, old: bytes, new: bytes) -> bytes:
    """A plain (not coded) header with old replaced by new, both CRCs fixed."""
    blob = bytearray(data)
    offset, size, _ = struct.unpack_from("<QQI", blob, 12)
    header = bytes(blob[32 + offset : 32 + offset + size])
    assert len(old) == len(new) and header.count(old) == 1
    header = header.replace(old, new)
    blob[32 + offset : 32 + offset + size] = header
    struct.pack_into("<I", blob, 28, zlib.crc32(header))
    struct.pack_into("<I", blob, 8, zlib.crc32(bytes(blob[12:32])))
    return bytes(blob)


def test_reads_what_7zip_wrote():
    """LZMA1 (no end mark: raw liblzma kept the last bytes back), BCJ + LZMA (7-Zip's coder
    order: LZMA first, BCJ bound to it), Delta + LZMA2, coded and plain headers."""
    with tempfile.TemporaryDirectory() as tmp:
        for name in FIXTURES:
            archive = sevenzip.SevenZip(_archive(Path(tmp), name))
            assert {m.name for m in archive.members} == set(FILES) | set(DIRS), name
            archive.extract(Path(tmp) / name)
            _assert_unpacked(Path(tmp) / name)


def test_bcj_is_undone():
    """dir/code.bin is x86 calls (E8 + offset): BCJ rewrites them, so a missed BCJ step shows."""
    with tempfile.TemporaryDirectory() as tmp:
        archive = sevenzip.SevenZip(_archive(Path(tmp), "bcj_lzma"))
        methods = [c.method for f in archive.streams.folders for c in f.coders]
        assert sevenzip.METHOD_X86 in methods and sevenzip.METHOD_LZMA in methods
        archive.extract(Path(tmp) / "out")
        assert (Path(tmp) / "out" / "dir" / "code.bin").read_bytes() == FILES["dir/code.bin"]


def test_refuses_names_outside_the_folder():
    data = base64.b64decode(FIXTURES["delta_lzma2"])
    evil = _rewrite_header(data, "dir/code.bin".encode("utf-16-le"), "..//code.bin".encode("utf-16-le"))
    with tempfile.TemporaryDirectory() as tmp:
        archive = sevenzip.SevenZip(_archive(Path(tmp), "delta_lzma2", evil))
        try:
            archive.extract(Path(tmp) / "out" / "x")
            raise AssertionError("no error")
        except sevenzip.SevenZipError as error:
            assert "outside" in str(error)
        assert not (Path(tmp) / "out").exists()  # refused before anything was written


def test_damage_is_an_error():
    data = base64.b64decode(FIXTURES["lzma"])
    with tempfile.TemporaryDirectory() as tmp:
        for bad, words in (
            (b"junk", "not a 7z archive"),
            (data[:200], "cut short"),
            (data[:40] + bytes([data[40] ^ 0xFF]) + data[41:], ""),  # file data: CRC or LZMA error
        ):
            try:
                sevenzip.SevenZip(_archive(Path(tmp), "lzma", bad)).extract(Path(tmp) / "out")
                raise AssertionError(f"no error ({words})")
            except sevenzip.SevenZipError as error:
                assert words in str(error), error


def test_unsupported_methods_say_so():
    f = sevenzip.Folder([sevenzip.Coder(b"\x03\x04\x01", b"\x06\x00\x00\x10\x00")], [], [0], [10])  # PPMd
    try:
        sevenzip.decode_folder(f, b"x")
        raise AssertionError("no error")
    except sevenzip.SevenZipError as error:
        assert "030401 is not read" in str(error)


def test_thirdparty_unpacks_a_7z_into_a_folder():
    """The pinned DNGHook .7z is unpacked by thirdparty.fetch (unpack=), and install.ps1
    gets the folder; a bad archive is a DownloadError, as for the zips."""
    assert thirdparty.TOOLS["dnghook"].unpack == "plugins/DriverNGHook.asi"
    with tempfile.TemporaryDirectory() as tmp:
        out = thirdparty.unpack(_archive(Path(tmp), "lzma"), Path(tmp) / "out")
        _assert_unpacked(out)
        try:
            thirdparty.unpack(_archive(Path(tmp), "lzma", b"junk"), Path(tmp) / "bad")
            raise AssertionError("no error")
        except thirdparty.DownloadError as error:
            assert "could not unpack" in str(error)


def test_the_pinned_driverng_hook_archive():
    """DNGHook_072.7z (LZMA2, solid, LZMA-coded header) where a copy is on disk; CI has none."""
    tool = thirdparty.TOOLS["dnghook"]
    copy = thirdparty.cached(tool)
    if copy is None:
        print(f"  (no {tool.file} under {REPO}: skipped)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        sevenzip.SevenZip(copy).extract(Path(tmp))
        asi = Path(tmp) / "plugins" / "DriverNGHook.asi"
        assert asi.stat().st_size == 791552 and asi.read_bytes()[:2] == b"MZ"
        assert len(list((Path(tmp) / "plugins" / "DriverNGHook").glob("*.lua"))) == 9


def test_dsff_install_hands_install_ps1_a_folder():
    """--hook-archive X.7z reaches install.ps1 unpacked, so its tar.exe never sees the .7z; a
    folder holding plugins/DriverNGHook.asi is passed on as it is (and passes preflight)."""
    import io

    from dsfforza.build import install

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = _archive(root, "lzma")
        hook_dir = root / "hook"
        (hook_dir / "plugins").mkdir(parents=True)
        (hook_dir / "plugins" / "DriverNGHook.asi").write_bytes(b"MZ")
        probes = install.Probes(
            running=lambda game: False, vc_runtime=lambda: True, free=lambda path: 1 << 50, loader=lambda game: "x.dll"
        )

        def hook_files(given: Path):
            args = install.build_parser().parse_args(["--hook-archive", str(given), "--out", str(root / "b")])
            run = install.Run(args, install.PlainReporter(io.StringIO()), probes)
            run.game, run.record = root / "game", None
            return run.hook_files()

        hook, loader = hook_files(archive)
        assert hook.is_dir() and loader is None
        _assert_unpacked(hook)
        assert hook_files(hook_dir) == (hook_dir, None)
        assert install.hook_source(hook_dir) and install.hook_source(archive)
        assert not install.hook_source(root) and not install.hook_source(root / "none.7z")
