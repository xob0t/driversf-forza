"""out/gentle (src/dsfforza/build/gentle.py): Idle priority for the process and every
process it starts, at most CORES jobs, and no CPU affinity (issue #325).

    uv run python tests/test_gentle.py      # or every test: uv run python -m tests
"""

from __future__ import annotations

import os
import subprocess
import sys

from dsfforza.build import gentle

# Run in a child (gentle.apply changes the process it runs in for good): apply, then report
# this process's priority class and affinity, and those of a child started Below Normal.
PROBE = r"""
import ctypes, os, subprocess, sys
from ctypes import wintypes
from dsfforza.build import gentle
k = ctypes.WinDLL("kernel32")
k.GetCurrentProcess.restype = wintypes.HANDLE
k.GetPriorityClass.argtypes = (wintypes.HANDLE,)
k.GetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_size_t))
def state():
    mine, system = ctypes.c_size_t(), ctypes.c_size_t()
    k.GetProcessAffinityMask(k.GetCurrentProcess(), ctypes.byref(mine), ctypes.byref(system))
    return k.GetPriorityClass(k.GetCurrentProcess()), mine.value == system.value
if sys.argv[1:] == ["child"]:
    k.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k.SetPriorityClass(k.GetCurrentProcess(), 0x4000)  # a worker asks for Below Normal
    print(*state())
else:
    print(gentle.apply(), *state())
    out = subprocess.run([sys.executable, "-c", sys.argv[1], "child"], capture_output=True, text=True,
                         creationflags=0x4000)
    print(out.stdout.strip())
"""


def test_gentle_is_idle_for_the_children_and_pins_no_cpu():
    if os.name != "nt":
        return
    env = {**os.environ, "DSFFORZA_GENTLE": "1"}
    out = subprocess.run([sys.executable, "-c", PROBE, PROBE], capture_output=True, text=True, env=env, check=True)
    me, child = out.stdout.split("\n")[:2]
    assert me.split() == ["True", str(gentle.IDLE), "True"], out.stdout + out.stderr  # Idle, every CPU
    assert child.split() == [str(gentle.IDLE), "True"], out.stdout + out.stderr  # held at Idle, every CPU


def test_gentle_caps_the_jobs():
    saved = os.environ.get("DSFFORZA_GENTLE")
    try:
        os.environ["DSFFORZA_GENTLE"] = "1"
        assert gentle.jobs(12) == gentle.CORES == 2 and gentle.creation_flags(0x4000) == (
            gentle.IDLE if os.name == "nt" else 0x4000
        )
        os.environ["DSFFORZA_GENTLE"] = "0"
        if not gentle.FLAG.exists():
            assert gentle.jobs(12) == 12 and gentle.creation_flags(0x4000) == 0x4000
    finally:
        if saved is None:
            os.environ.pop("DSFFORZA_GENTLE", None)
        else:
            os.environ["DSFFORZA_GENTLE"] = saved


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
