"""One job pool for every heavy process on this PC, whatever worktree or tool starts it
(research/batch.md, "Job pool").

Several agents build and survey at once, each in its own worktree. Each build used to
start a worker per logical CPU, so three of them ran 48 processes on 16 CPUs, and the
test VMs' game loads starved (issue #70). Now every conversion worker, lint process,
garage-picture render process and survey process takes a slot of one pool first.

The pool is a folder (POOL_ENV, else %LOCALAPPDATA%\\dsfforza\\pool) of slot files,
slot-NN.lock. A process holds a slot while it holds a lock on its file (a byte past the
end, so the file's text, who holds it, stays readable). The OS drops the lock when the
process dies, so a killed build never keeps its slots. Taking a slot happens under
pool.lock, so two processes can't both take the last one.

How many slots are handed out (capacity()):

- one per logical CPU, as many as WORKER_RAM each fits in the PC's RAM
  (port_batch.default_jobs, the figure a build used for itself);
- at most gentle.CORES while out/gentle exists, in this worktree or the main checkout;
- minus the CPUs a test run reserves while its game loads (holds, below).

Processes that find no free slot wait in line (queue/): a slot that comes free goes to
the one that has waited longest, so two builds share the pool rather than the first
keeping every slot it frees.

A new slot is taken only while the RAM its job needs is free (the per-job figures of
port_batch: WORKER_RAM, RENDER_RAM, LINT_RAM), unless the process holds none yet: every
build gets at least one slot as the pool frees, as it got one worker before.

Holds. tools\\wsb\\run-series.ps1 (Wait-CpuGate) writes holds\\series-<pid>.json before it
launches the game: {"pid", "since", "until", "cpus", "runs", "prefix"}. While it holds,
the pool hands out `cpus` fewer slots (the VM's vCPUs; all of them for a host run), and
the gate waits until that many are free. The hold ends when the run's events.json (in
<runs>\\<prefix>*, a folder made after "since") says the Story world is ready, when the
run has a result.json, at "until", or when the series' process is gone.

    uv run dsff jobpool status        # slots, holders, holds
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time
from pathlib import Path

POOL_ENV = "DSFFORZA_POOL"  # the pool folder; "off" turns the pool off (each build on its own, as before)
LOCK_AT = 1 << 30  # the byte each slot file is locked at: past the end, so its text can be read
POLL = 0.5  # seconds between tries while waiting for a slot
READY_TEXT = "story world ready"  # run-test.ps1 Note "story world ready after N presses" (events.json)
_HELD: list = []  # this process's slots (Slot), so a process holding none gets one past the RAM check


def root() -> Path | None:
    """The pool folder, or None when the pool is off."""
    value = os.environ.get(POOL_ENV)
    if value is not None:
        return None if value.strip().lower() in ("off", "0", "") else Path(value)
    base = os.environ.get("LOCALAPPDATA") or (Path.home() / ".cache")
    return Path(base) / "dsfforza" / "pool"


# --- locks --------------------------------------------------------------------------------------


def _try_lock(fd: int) -> bool:
    os.lseek(fd, LOCK_AT, os.SEEK_SET)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    os.lseek(fd, LOCK_AT, os.SEEK_SET)
    with contextlib.suppress(OSError):
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)


def _open(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOINHERIT", 0), 0o666)


@contextlib.contextmanager
def _pool_lock(folder: Path, timeout: float = 30.0):
    """pool.lock, held while slots are counted and taken (a few milliseconds)."""
    fd = _open(folder / "pool.lock")
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(fd):
            if time.monotonic() > deadline:  # a holder that hangs: go on without (a slot more at worst)
                break
            time.sleep(0.02)
        yield
    finally:
        _unlock(fd)
        os.close(fd)


def _slot_busy(path: Path) -> bool:
    """Whether another process holds the slot file (this process's own count too)."""
    try:
        fd = _open(path)
    except OSError:
        return True
    try:
        if _try_lock(fd):
            _unlock(fd)
            return False
        return True
    finally:
        os.close(fd)


# --- sizing ----------------------------------------------------------------------------------


def size(cpus: int | None = None, ram: int | None = None, *, gentle_on: bool | None = None) -> int:
    """Slots without holds: port_batch.default_jobs (a slot per logical CPU, as many as
    WORKER_RAM each fits in the RAM), at most gentle.CORES while out/gentle is on."""
    from dsfforza.build import gentle, port_batch

    n = port_batch.default_jobs(cpus, ram)
    on = gentle.anywhere() if gentle_on is None else gentle_on
    return min(n, gentle.CORES) if on else n


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _world_ready(runs: str, prefix: str, since: float) -> bool:
    """A run folder <runs>\\<prefix>* made after `since` whose game reached the Story world
    (events.json) or that has its result (result.json: done, loaded or not)."""
    try:
        folders = [e for e in os.scandir(runs) if e.name.startswith(prefix) and e.is_dir()]
    except OSError:
        return False
    for e in folders:
        try:
            if e.stat().st_mtime < since - 2:
                continue
            folder = Path(e.path)
            if (folder / "result.json").exists():
                return True
            events = folder / "events.json"
            if events.exists() and READY_TEXT in events.read_text(encoding="utf-8-sig", errors="replace").lower():
                return True
        except OSError:
            continue
    return False


def holds(folder: Path | None = None, now: float | None = None) -> list[dict]:
    """The active holds (see the module docstring); ended ones are removed."""
    folder = folder if folder is not None else root()
    if folder is None:
        return []
    now = time.time() if now is None else now
    out = []
    try:
        paths = sorted((folder / "holds").glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            hold = json.loads(path.read_text(encoding="utf-8-sig"))
            pid, since, until = int(hold["pid"]), float(hold["since"]), float(hold["until"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        ended = (
            now > until
            or not _alive(pid)
            or (hold.get("runs") and _world_ready(str(hold["runs"]), str(hold.get("prefix", "")), since))
        )
        if ended:
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        out.append({**hold, "file": path.name})
    return out


def capacity(folder: Path | None = None, total: int | None = None) -> int:
    """Slots that may be busy now: size() minus the CPUs the active holds reserve."""
    total = size() if total is None else total
    reserved = 0
    for hold in holds(folder):
        try:
            reserved += int(hold.get("cpus", total))
        except (TypeError, ValueError):
            reserved += total
    return max(0, total - reserved)


# --- slots -----------------------------------------------------------------------------------


class Slot:
    """One slot of the pool, held until release() or the process's end."""

    def __init__(self, path: Path | None, fd: int | None, label: str):
        self.path, self.fd, self.label = path, fd, label

    def release(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.ftruncate(self.fd, 0)
            _unlock(self.fd)
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None
        if self in _HELD:
            _HELD.remove(self)

    def __enter__(self) -> Slot:
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def held() -> int:
    """Slots this process holds."""
    return len(_HELD)


# The queue: a process that found no free slot waits in line (queue/<pid>.lock, locked while it
# waits, its text the time it joined). A free slot goes to the earliest waiter, so a build
# that just freed a slot doesn't take it again before a build that has been waiting.
_TICKET: list = []  # [fd, path, since] while this process waits


def _join_queue(folder: Path) -> None:
    if _TICKET:
        return
    queue = folder / "queue"
    queue.mkdir(exist_ok=True)
    path = queue / f"{os.getpid()}.lock"
    fd = _open(path)
    if not _try_lock(fd):
        os.close(fd)
        return
    since = time.time()
    with contextlib.suppress(OSError):
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "since": since}).encode())
    _TICKET.extend((fd, path, since))


def leave_queue() -> None:
    """Stop waiting in line (try_acquire does when it gets a slot)."""
    if not _TICKET:
        return
    fd, path, _ = _TICKET
    _TICKET.clear()
    _unlock(fd)
    with contextlib.suppress(OSError):
        os.close(fd)
    with contextlib.suppress(OSError):
        path.unlink()


def _ahead(folder: Path) -> int:
    """Waiters of other processes that joined the queue before this one (before now, when
    this one doesn't wait)."""
    mine = _TICKET[2] if _TICKET else time.time()
    own = _TICKET[1] if _TICKET else None
    n = 0
    try:
        tickets = list((folder / "queue").glob("*.lock"))
    except OSError:
        return 0
    for path in tickets:
        if path == own or not _slot_busy(path):  # not waiting any more (its process ended)
            continue
        try:
            since = float(json.loads(path.read_text(encoding="utf-8") or "{}").get("since", 0))
        except (OSError, ValueError, AttributeError):
            continue
        if since < mine:
            n += 1
    return n


def try_acquire(
    label: str,
    ram: int = 0,
    free=None,
    folder: Path | None = None,
    total: int | None = None,
    *,
    queue: bool = True,
) -> Slot | None:
    """A slot now, or None: the pool is at capacity (or holds reserve it), the free slots
    go to processes that have waited longer, or this process holds a slot already and `ram`
    bytes aren't free (free(): bytes free now; default port_batch.free_ram). Without a slot
    the process joins the queue (unless `queue` is False: it will not ask again soon). With
    the pool off, always a slot (that holds nothing)."""
    folder = folder if folder is not None else root()
    if folder is None:
        return Slot(None, None, label)
    if ram and _HELD:
        if free is None:
            from dsfforza.build.port_batch import free_ram as free
        available = free()
        if available is not None and available < ram:
            return None
    total = size() if total is None else total
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with _pool_lock(folder):
            cap = capacity(folder, total)
            paths = [folder / f"slot-{i:02d}.lock" for i in range(total)]
            busy = [p for p in paths if _slot_busy(p)]
            if cap - len(busy) <= _ahead(folder):
                if queue:
                    _join_queue(folder)
                return None
            for path in paths:
                if path in busy:
                    continue
                fd = _open(path)
                if not _try_lock(fd):
                    os.close(fd)
                    continue
                info = {
                    "pid": os.getpid(),
                    "label": label,
                    "since": round(time.time(), 1),
                    "ram": ram,
                    "cwd": str(Path.cwd()),
                    "argv": sys.argv[:3],
                }
                with contextlib.suppress(OSError):
                    os.lseek(fd, 0, os.SEEK_SET)
                    os.ftruncate(fd, 0)
                    os.write(fd, json.dumps(info).encode())
                slot = Slot(path, fd, label)
                _HELD.append(slot)
                leave_queue()
                return slot
    except OSError as error:  # an unusable pool folder: run as before rather than not at all
        print(f"job pool: {folder}: {error}; going on without it", file=sys.stderr)
        return Slot(None, None, label)
    return None


def acquire(label: str, ram: int = 0, poll: float = POLL, log=None, **kwargs) -> Slot:
    """A slot, waiting for one as long as it takes. `log`: told once when it has to wait."""
    said = False
    while True:
        slot = try_acquire(label, ram, **kwargs)
        if slot is not None:
            return slot
        if log is not None and not said:
            log(f"job pool: waiting for a slot ({label})")
            said = True
        time.sleep(poll)


def lease(label: str, want: int, ram: int = 0, log=None) -> list[Slot]:
    """Up to `want` slots: one waited for, the others as free now (for a step that starts
    its processes at once, like port_batch.lint_all)."""
    first = acquire(label, ram, log=log)
    out = [first]
    while len(out) < want:
        slot = try_acquire(label, ram, queue=False)
        if slot is None:
            break
        out.append(slot)
    return out


# --- process pools ----------------------------------------------------------------------------


def _start_process(label: str, ram: int, init, initargs: tuple) -> None:
    """A pool process's initializer: its slot first (held until the process ends), then the
    caller's initializer. The executor hands work only to processes past this."""
    acquire(label, ram)
    if init is not None:
        init(*initargs)


def executor(jobs: int, label: str, ram: int = 0, initializer=None, initargs: tuple = (), **kwargs):
    """A ProcessPoolExecutor of up to `jobs` processes, each holding a pool slot for its
    life: a process waits for its slot before it takes work, and its slot goes with it."""
    from concurrent.futures import ProcessPoolExecutor

    return ProcessPoolExecutor(
        max_workers=jobs, initializer=_start_process, initargs=(label, ram, initializer, initargs), **kwargs
    )


def mp_pool(jobs: int, label: str, ram: int = 0, initializer=None, initargs: tuple = ()):
    """multiprocessing.Pool, each process holding a pool slot (as executor())."""
    from multiprocessing import Pool

    return Pool(jobs, initializer=_start_process, initargs=(label, ram, initializer, initargs))


# --- children that die with their parent -----------------------------------------------------------

_JOB: list = []


def die_with_me(proc) -> None:
    """Put a child process (subprocess.Popen) in a job object that ends it when this process
    ends: its slot is this process's, and it must not run on without it (Windows only)."""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    if not _JOB:

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

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        info = Extended()
        info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):  # extended limits
            return
        _JOB.append(job)
    with contextlib.suppress(AttributeError, OSError):
        kernel32.AssignProcessToJobObject(_JOB[0], wintypes.HANDLE(int(proc._handle)))


# --- status ------------------------------------------------------------------------------------


def status(folder: Path | None = None, total: int | None = None) -> dict:
    folder = folder if folder is not None else root()
    if folder is None:
        return {"pool": None}
    total = size() if total is None else total
    slots = []
    for i in range(total):
        path = folder / f"slot-{i:02d}.lock"
        if not path.exists() or not _slot_busy(path):
            continue
        try:
            info = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            info = {}
        slots.append({"slot": i, **info})
    return {
        "pool": str(folder),
        "size": total,
        "capacity": capacity(folder, total),
        "busy": slots,
        "holds": holds(folder),
    }


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="dsff jobpool", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="slots, their holders, and the test runs' holds")
    s.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    st = status()
    if args.json:
        print(json.dumps(st, indent=1))
        return
    if st["pool"] is None:
        print(f"job pool off ({POOL_ENV})")
        return
    print(f"{st['pool']}: {len(st['busy'])} of {st['capacity']} slots busy (size {st['size']})")
    for s_ in st["busy"]:
        age = time.time() - s_.get("since", time.time())
        print(f"  slot {s_['slot']:2d}: pid {s_.get('pid')} {s_.get('label', '')} ({age:.0f} s) {s_.get('cwd', '')}")
    for h in st["holds"]:
        print(
            f"  hold {h['file']}: {h.get('cpus')} CPUs for pid {h.get('pid')} until the world loads ({h.get('prefix')})"
        )


if __name__ == "__main__":
    main()
