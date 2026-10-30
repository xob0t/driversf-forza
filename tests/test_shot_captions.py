"""Probe shot captions (tools/wsb/guest/run-test.ps1): the caption names the car and view from
clonetest.lua's shot state, and lights_diff leaves the caption strip out. Offline, a few seconds.

uv run python -m tests shot_captions
"""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

from dsfforza import REPO
from dsfforza.harness import lights_diff

RUN_TEST = REPO / "tools" / "wsb" / "guest" / "run-test.ps1"

# Loads run-test.ps1's caption functions (not the script: it would build a run folder and start the
# game) and prints the captions for a few shot states.
CAPTIONS = r"""
param($Script, $Tmp)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
$names = 'Set-CarNames', 'Read-ShotState', 'Get-CarText', 'Get-ShotCaption'
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in $names }, $true)) {
    . ([scriptblock]::Create($f.Extent.Text))
}
$RepoDir = $Tmp; $RunPath = "$Tmp\run"; $carNames = @{}; $ShotLabel = 'after: out\b\dngvehicles.sp'; $log = "$RunPath\Driver.log"
Set-CarNames "$Tmp\dngvehicles.lua"
# No shot line yet (an older probe): the latest "id N:" line, marked as lagging.
Get-ShotCaption 'observe-01.png' (Read-ShotState) (Read-ShotState)
Add-Content $log '[Lua] [dsfforza] shot 4: id=1 probe=1 view=left+lit phase=extra_view'
$state = Read-ShotState
Get-ShotCaption 'observe-07.png' $state $state
# The log is read on from where the last call stopped.
Add-Content $log "[Lua] [dsfforza] shot 5: id=266 probe=266 view=cockpit phase=cockpit_view`n[Lua] [dsfforza] id 266: cockpit view on"
Get-ShotCaption 'observe-08.png' $state (Read-ShotState)
Get-ShotCaption 'observe-09.png' @{ seq = '6' } @{ seq = '6'; id = ''; probe = '2'; view = 'chase' }
"""


def test_caption_names_car_view_and_archive():
    (REPO / "out").mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="test-shot-captions-", dir=REPO / "out"))
    try:
        (tmp / "research").mkdir()
        (tmp / "research" / "vehicles.tsv").write_text(
            "vehc\tspool_id\tclass\tmodel\n3\t266\tSaloon 15\tASYM_Desanne_2007_cop|ASYM_Desanne_2007_Civ\n"
        )
        (tmp / "research" / "hosts.tsv").write_text("id\tclass\tmodel\n266\tSaloon 15\tASYM_Desanne_2007_Civ\n")
        (tmp / "dngvehicles.lua").write_text(
            'local ported = {\n\tcars = {\n\t\t[1] = { host = 205, media = "BMW_M3E30_91", name = "BMW M3E30 (1991)", '
            'make = "BMW" },\n\t},\n}\nreturn ported\n'
        )
        (tmp / "run").mkdir()
        (tmp / "run" / "Driver.log").write_text("[Lua] [dsfforza] id 266: view left on\n")
        script = tmp / "captions.ps1"
        script.write_text(CAPTIONS)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Script", str(RUN_TEST), "-Tmp", str(tmp)],
            check=False, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        lines = result.stdout.splitlines()
        assert result.returncode == 0 and len(lines) == 4, result.stdout + result.stderr
        sep = "  |  "
        label = "after: out\\b\\dngvehicles.sp"
        assert lines[0] == sep.join(
            ["observe-01", "ID 266 ASYM_Desanne_2007_Civ (vanilla) (from Driver.log, may lag)", label]
        )
        assert lines[1] == sep.join(
            ["observe-07", "ID 1 BMW M3E30 (1991) [BMW_M3E30_91] (ported, host 205)", "view left+lit", label]
        )
        # The state changed between the reads before and after the frame copy: both are named.
        assert lines[2] == sep.join(
            ["observe-08", "ID 266 ASYM_Desanne_2007_Civ (vanilla)", "view cockpit",
             "changed during the shot (was ID 1, view left+lit)", label]
        )  # fmt: skip
        assert lines[3] == sep.join(["observe-09", "no car (probe ID 2 next)", "view chase", label])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _png(path: Path, w: int, h: int, rows: list[bytes]) -> None:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + row for row in rows)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )  # fmt: skip


def test_lights_diff_leaves_the_caption_out():
    tmp = Path(tempfile.mkdtemp(prefix="test-shot-captions-"))
    try:
        w, frame, caption = 4, 3, 2
        rows = [bytes([10] * w * 3)] * frame + [bytes([255] * w * 3)] * caption
        _png(tmp / "shot.png", w, frame + caption, rows)
        assert lights_diff.caption_rows(tmp) == 0  # no result.json: an older run, no strip
        (tmp / "result.json").write_text(json.dumps({"shotCaptionPx": caption}))
        assert lights_diff.caption_rows(tmp) == caption
        assert lights_diff.read_frame(tmp / "shot.png", caption) == (w, frame, bytearray([10] * w * frame * 3))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
