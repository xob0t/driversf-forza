"""Low-impact mode for builds while the PC is in use (e.g. the user is gaming).

On while the file out/gentle exists (or DSFFORZA_GENTLE=1): the calling
process drops to Idle priority and is pinned to the last CORES logical CPUs.
Windows passes both to child processes, so port_batch workers and lints
inherit them; port_batch also caps --jobs at CORES.

    touch out/gentle   # on
    rm out/gentle      # off
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

FLAG = Path(__file__).resolve().parent.parent / "out" / "gentle"
CORES = 2
IDLE = 0x00000040


def enabled() -> bool:
    return FLAG.exists() or os.environ.get("DSFFORZA_GENTLE") == "1"


def apply() -> bool:
    """Idle priority and the last CORES CPUs for this process, when enabled."""
    if not enabled():
        return False
    try:
        if os.name == "nt":
            from ctypes import wintypes
            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.SetProcessAffinityMask.argtypes = (wintypes.HANDLE, ctypes.c_size_t)
            handle = kernel32.GetCurrentProcess()
            kernel32.SetPriorityClass(handle, IDLE)
            count = os.cpu_count() or CORES
            mask = 0
            for cpu in range(max(0, count - CORES), count):
                mask |= 1 << cpu
            kernel32.SetProcessAffinityMask(handle, mask)
        else:
            os.nice(19)
            count = os.cpu_count() or CORES
            os.sched_setaffinity(0, set(range(max(0, count - CORES), count)))
    except (OSError, AttributeError):
        pass
    return True


def creation_flags(default: int) -> int:
    """Popen creationflags: Idle when enabled (an explicit class would override inheritance)."""
    return IDLE if enabled() and os.name == "nt" else default


def jobs(requested: int) -> int:
    return min(requested, CORES) if enabled() else requested
