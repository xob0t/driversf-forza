"""tools/wsb/vm-common.ps1: the settings a test VM's session runs with, read from its vmware.log
(issue #610: a VM started by a checkout with older $VmSettings keeps them until it is powered off).
Offline, a few seconds.

uv run python -m tests vm_settings
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from dsfforza import REPO

VM_COMMON = REPO / "tools" / "wsb" / "vm-common.ps1"

PREFIX = '2026-11-13T23:00:15.940Z -INFO vmware-vmx.exe 44312 [ws@4413 threadName="vmx"] '
LOG = [
    "DICT --- USER DEFAULTS C:\\Users\\admin\\AppData\\Roaming\\VMware\\config.ini",
    'DICT                   memsize = "8192"',  # a default, not the VM's
    "DICT --- CONFIGURATION C:\\VMs\\dsfforza-test\\dsfforza-test.vmx ",
    'DICT            config.version = "8"',
    'DICT                   memsize = "4096"',
    'DICT     svga.graphicsMemoryKB = "524288"',
    'DICT                  numvcpus = "4"',
    'DICT      cpuid.coresPerSocket = "4"',
    "DICT --- USER DEFAULTS C:\\Users\\admin\\AppData\\Roaming\\VMware\\config.ini ",
    'DICT                   memsize = "2048"',
    "VMMEM: Initial Reservation: 7341MB (MainMem=4096MB)",
]


def _run(log: list[str] | None, script: str) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        if log is not None:
            Path(tmp, "vmware.log").write_text("".join(PREFIX + line + "\n" for line in log), encoding="utf-8")
        command = f". '{VM_COMMON}'; $script:VmDir = '{tmp}'; {script}"
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
            check=False, capture_output=True, text=True, timeout=60,
        )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.splitlines()


def test_session_settings_come_from_the_vmx_block_of_the_log():
    got = _run(LOG, '(Get-VmSessionSettings).GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }')
    assert sorted(got) == sorted(
        ["memsize=4096", "svga.graphicsMemoryKB=524288", "numvcpus=4", "cpuid.coresPerSocket=4"]
    )


def test_stale_settings_name_each_key_the_session_has_otherwise():
    got = _run(
        LOG, "$VmSettings['memsize'] = '6144'; $VmSettings['svga.graphicsMemoryKB'] = '1048576'; Get-VmStaleSettings"
    )
    assert got == ["memsize = 4096 (wanted 6144)", "svga.graphicsMemoryKB = 524288 (wanted 1048576)"]


def test_a_session_with_the_current_settings_or_no_log_is_not_stale():
    settings = _settings()
    assert set(settings) == {"memsize", "svga.graphicsMemoryKB", "numvcpus", "cpuid.coresPerSocket"}
    log = ["DICT --- CONFIGURATION x.vmx"] + [f'DICT {key} = "{value}"' for key, value in settings.items()]
    script = "'stale ' + @(Get-VmStaleSettings).Count"
    assert _run(log, script) == ["stale 0"]
    assert _run(None, script) == ["stale 0"]


def _settings() -> dict[str, str]:
    lines = _run(None, '$VmSettings.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }')
    return dict(line.split("=", 1) for line in lines)
