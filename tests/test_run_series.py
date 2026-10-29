"""tools/wsb/run-series.ps1: an archive past the vanilla reader (1100 root entries, 2 GiB)
without -Asi stops before the series waits for a VM (issue #224). Offline, a few seconds.

uv run python -m tests run_series
"""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

from dsfforza import REPO

SCRIPT = REPO / "tools" / "wsb" / "run-series.ps1"


def _run_series(archive: Path, *extra: str) -> subprocess.CompletedProcess:
    # -VmId names no VM: should the check let the archive through, the series fails at the
    # pool instead of taking one of this PC's test VMs.
    return subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
         "-Archives", str(archive.relative_to(REPO)), "-Vm", "-VmId", "no-such-vm",
         "-GamePath", str(archive.parent), *extra],
        check=False, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


def _archive(folder: Path, entries: int) -> Path:
    path = folder / "dngvehicles.sp"
    path.write_bytes(b"CHNK" + struct.pack("<III", 16, entries, 3))
    return path


def test_archive_past_the_vanilla_reader_needs_asi():
    (REPO / "out").mkdir(exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="test-run-series-", dir=REPO / "out"))
    try:
        result = _run_series(_archive(folder, 1101))
        assert result.returncode != 0
        assert "1101 root entries" in result.stdout + result.stderr, result.stdout + result.stderr
        # -NoAsi (and an archive the vanilla reader takes) pass the check and reach the VM pool.
        for archive, extra in ((_archive(folder, 1101), ("-NoAsi",)), (_archive(folder, 1100), ())):
            result = _run_series(archive, *extra)
            assert result.returncode != 0
            output = result.stdout + result.stderr
            assert "root entries" not in output and "no-such-vm" in output, output
    finally:
        shutil.rmtree(folder, ignore_errors=True)
