"""run-test.ps1's Resume-PausedWorld (issue #589): after the probe's go, Esc only while the probe's Lua
is silent (a paused world), never while it logs. Offline, about 15 s.

uv run python -m tests unpause
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from dsfforza import REPO

RUN_TEST = REPO / "tools" / "wsb" / "guest" / "run-test.ps1"

# Loads Resume-PausedWorld from run-test.ps1 (not the script: it would start the game) with the game,
# the key press and the shots stubbed. A background job writes the probe's lines as the scenario says.
HARNESS = r"""
param($Script, $Tmp, $Scenario)
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Script, [ref]$null, [ref]$null)
$f = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Resume-PausedWorld' }, $true)
. ([scriptblock]::Create($f[0].Extent.Text))
Add-Type -TypeDefinition 'public static class DsfGuest { public static int Taps; public static void Tap(System.IntPtr w, byte vk, byte scan) { if (vk == 0x1B) Taps++; } }'
function Save-Shot($Name) { }
function Get-GameWindow { [IntPtr]::Zero }
function Note($m) { "note: $m" }
$driver = [pscustomobject]@{ HasExited = $false } | Add-Member -MemberType ScriptMethod -Name Refresh -Value { } -PassThru
$RunPath = $Tmp; $PauseQuietSeconds = 3; $result = @{}
$log = Join-Path $Tmp 'Driver.log'
Set-Content $log '[Lua] [dsfforza] zap level 3, inZap true'
$writer = Start-Job -ArgumentList $log, $Scenario -ScriptBlock {
    param($log, $scenario)
    if ($scenario -eq 'running') {
        # The Lua logs every ~1 s, then sees the go.
        for ($i = 0; $i -lt 6; $i++) { Start-Sleep -Milliseconds 900; Add-Content $log '[Lua] [dsfforza] zap level 1, inZap true' }
        Add-Content $log '[Lua] [dsfforza] harness go after 12 ticks (zap level 1)'
    } else {
        # Paused: silent until well after the quiet time, then (resumed) it goes on.
        Start-Sleep -Seconds 6
        Add-Content $log "[Lua] [dsfforza] zap level 1, inZap true`r`n[Lua] [dsfforza] harness go after 30 ticks (zap level 1)"
    }
}
Resume-PausedWorld
Wait-Job $writer | Remove-Job
"taps $([DsfGuest]::Taps) unpause $($result.unpause)"
"""


def _run(scenario: str) -> list[str]:
    (REPO / "out").mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="test-unpause-", dir=REPO / "out"))
    try:
        script = tmp / "unpause.ps1"
        script.write_text(HARNESS)
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
             "-Script", str(RUN_TEST), "-Tmp", str(tmp), "-Scenario", scenario],
            check=False, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.splitlines()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_no_esc_while_the_probe_logs():
    lines = _run("running")
    assert lines[-1] == "taps 0 unpause ", lines


def test_esc_once_when_the_probe_is_silent():
    lines = _run("paused")
    assert lines[-1] == "taps 1 unpause 1", lines
    assert any("Esc (1)" in line for line in lines) and any("goes on after 1 Esc" in line for line in lines), lines
