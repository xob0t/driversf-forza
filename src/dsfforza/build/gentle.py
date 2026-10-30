"""Low-impact mode for builds while the PC is in use (e.g. the user is gaming).

On while the file out/gentle exists (or DSFFORZA_GENTLE=1): the calling process and
every process it starts run at Idle priority, and port_batch runs at most CORES
workers (jobs). No CPU affinity: Windows spreads the Idle processes over whatever
cores are free, and Idle threads only get CPU time no thread of a higher priority
wants. (Until issue #325 every gentle process was pinned to the last two logical
CPUs, so every agent's gentle builds queued on the same two while the rest idled.)

On Windows the process joins a job object whose priority-class limit is Idle, so its
children stay at Idle even when they ask for another class (port_batch workers set
Below Normal on themselves). Elsewhere it is nice 19, which children inherit.

    touch out/gentle   # on
    rm out/gentle      # off
"""

from __future__ import annotations

import ctypes
import os

from dsfforza import REPO

FLAG = REPO / "out" / "gentle"
CORES = 2  # processes doing heavy work at once (port_batch --jobs)
IDLE = 0x00000040
BELOW_NORMAL = 0x00004000
JOB_OBJECT_LIMIT_PRIORITY_CLASS = 0x00000020
JOB_OBJECT_BASIC_LIMIT_INFORMATION = 2
_JOB: list = []  # the job object's handle, kept open for the life of the process


def enabled() -> bool:
    return FLAG.exists() or os.environ.get("DSFFORZA_GENTLE") == "1"


def apply() -> bool:
    """Idle priority for this process and its children, when enabled."""
    if not enabled():
        return False
    try:
        if os.name == "nt":
            _idle_job()
        else:
            os.nice(19)
    except (OSError, AttributeError):
        pass
    return True


def set_priority(priority_class: int) -> bool:
    """This process at a Windows priority class (IDLE, BELOW_NORMAL). False if that failed or
    this isn't Windows. The handle needs its real type: GetCurrentProcess()'s -1 passed as a
    C int is not the pseudo-handle, and SetPriorityClass fails with ERROR_INVALID_HANDLE."""
    if os.name != "nt":
        return False
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), priority_class))


def _idle_job() -> None:
    """This process at Idle, in a job object that holds it and its children at Idle."""
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    handle = kernel32.GetCurrentProcess()
    kernel32.SetPriorityClass(handle, IDLE)  # also if the job below can't be made
    if _JOB:
        return
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return
    limits = BasicLimits(LimitFlags=JOB_OBJECT_LIMIT_PRIORITY_CLASS, PriorityClass=IDLE)
    if kernel32.SetInformationJobObject(
        job, JOB_OBJECT_BASIC_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ) and kernel32.AssignProcessToJobObject(job, handle):
        _JOB.append(job)


def creation_flags(default: int) -> int:
    """Popen creationflags: Idle when enabled (an explicit class would override inheritance)."""
    return IDLE if enabled() and os.name == "nt" else default


def jobs(requested: int) -> int:
    return min(requested, CORES) if enabled() else requested
