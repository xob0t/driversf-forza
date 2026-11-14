"""Run one command in a slot of the PC's job pool and report its peak memory
(research/web-builder.md; the pool: research/batch.md, "Job pool").

    uv run python tools/web-builder/pool_run.py --ram-mb 3300 --label web-car -- node ...

The child is adopted by the slot (jobpool.adopt: it dies with this process and its memory
counts against the slot) and runs in jobpool's job object, so its peak is the job object's
PeakProcessMemoryUsed: committed bytes, the same measure as port_batch.WORKER_RAM's
("RAM measured" in research/batch.md). Prints {"code", "seconds", "peak_commit_bytes"}.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes

from dsfforza.build import jobpool


def job_peak() -> int | None:
    """PeakProcessMemoryUsed of jobpool's job object (Windows), or None."""
    if not jobpool._JOB:
        return None

    class Basic(ctypes.Structure):
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

    class Extended(ctypes.Structure):
        _fields_ = [
            ("Basic", Basic),
            ("Io", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.QueryInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    info = Extended()
    if not kernel32.QueryInformationJobObject(jobpool._JOB[0], 9, ctypes.byref(info), ctypes.sizeof(info), None):
        return None
    return int(info.PeakProcessMemoryUsed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ram-mb", type=int, default=3300, help="the slot's RAM reservation (MiB)")
    parser.add_argument("--label", default="web-builder")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    slot = jobpool.acquire(args.label, args.ram_mb << 20, child=True, log=print)
    try:
        started = time.time()
        proc = subprocess.Popen(command, creationflags=subprocess.BELOW_NORMAL_PRIORITY_CLASS)
        jobpool.adopt(slot, proc)
        code = proc.wait()
        result = {"code": code, "seconds": round(time.time() - started, 1), "peak_commit_bytes": job_peak()}
    finally:
        slot.release()
    print(json.dumps(result))
    return 0 if code == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
