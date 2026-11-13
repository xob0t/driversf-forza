"""tools/wsb/guest/guest-game.ps1: what of the host's game folder a test guest gets (issue #589), and
run-test.ps1's copy of dsf_enhanced.ini. Offline, a few seconds.

uv run python -m tests guest_game
"""

from __future__ import annotations

import subprocess

from dsfforza import REPO

GUEST_GAME = REPO / "tools" / "wsb" / "guest" / "guest-game.ps1"

INI = """[General]
Log=1
[Quality]
LODEngineThresholdScale=0.25
TrafficSpawnDistanceScale=2.0
[Ultrawide]
Enabled=1
[DLSS5]
Export=1
Helper=1
HelperBypass=1
[DLAA]
; DLAA
Enable=1
DisableMLAA=
"""


def _run(script: str) -> list[str]:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", f". '{GUEST_GAME}'; {script}"],
        check=False, capture_output=True, text=True, timeout=60,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


def test_guest_gets_the_retail_game_without_graphics_proxies():
    files = ["Driver.exe", "DFEngine.dll", "winmm.dll", "dsf_enhanced.asi", "dsf_enhanced.ini",
             "d3d9.dll", "D3D9.DLL", "dxgi.dll", "dxvk.conf", "winmm.dll.bak-before-dlss5", "dsf_enhanced.asi.bak",
             "Driver.log", "Driver_20261105_063058.dmp"]  # fmt: skip
    folders = ["Common", "Install", "media", "Locale", "Input", "bin", "plugins",
               "dlss5", "dsf_dlss5_dumps", "dsf_enhanced", "worldlod-map", "dsfcars", "dsfcars-backup",
               "Felony_Rework_2.3_Hotfix_1"]  # fmt: skip
    script = "; ".join(
        [f"'{name} ' + (Test-GuestGameFile '{name}')" for name in files]
        + [f"'{name} ' + (Test-GuestGameFolder '{name}')" for name in folders]
    )
    got = dict(line.rsplit(" ", 1) for line in _run(script))
    assert [name for name in files if got[name] == "True"] == [
        "Driver.exe", "DFEngine.dll", "winmm.dll", "dsf_enhanced.asi", "dsf_enhanced.ini",
    ]  # fmt: skip
    assert [name for name in folders if got[name] == "True"] == [
        "Common", "Install", "media", "Locale", "Input", "bin", "plugins",
    ]  # fmt: skip


def test_run_ini_turns_nvidia_features_off_in_a_guest_only():
    lines = "@(" + ", ".join("'" + line + "'" for line in INI.splitlines()) + ")"
    guest = _run(f"Get-RunEnhancedIni {lines} -Guest")
    on_host = _run(f"Get-RunEnhancedIni {lines}")
    expected = INI.splitlines()
    expected[3] = "LODEngineThresholdScale=1.0"
    expected[4] = "TrafficSpawnDistanceScale=1.0"
    assert on_host == expected
    expected[8], expected[9], expected[13] = "Export=0", "Helper=0", "Enable=0"
    # [Ultrawide] Enabled, HelperBypass and the comment stay as they are.
    assert guest == expected
