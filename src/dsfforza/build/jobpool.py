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

RAM (research/batch.md, "Job pool", "RAM"). A slot is taken for a job of a kind, with
that kind's measured peak (port_batch.WORKER_RAM, LINT_RAM, RENDER_RAM, DIFF_RAM, ...),
and it reserves that much for as long as it is held. A slot is granted only while

    the host's available RAM - what busy slots' jobs may still grow - what the test VMs
        that series hold still need - SPARE (the owner's programs) - what earlier waiters
        want  >=  the peak

Every term is read at grant time, under pool.lock: the host's available RAM
(GlobalMemoryStatusEx: the owner's programs and any running VM count as they are now), the
private working set of each busy slot's job (its peak less that is what it may still take),
and for each VM a series holds (RAM holds, below), VM_RAM less what that VM's processes hold
now. So the host's available RAM stays above SPARE plus the held VMs' unmet need, whatever
builds run in whichever worktree, as long as each job stays under its kind's peak. Every
slot is checked, a process's first too.

Holds. tools\\wsb\\run-series.ps1 writes two kinds to holds\\:

- CPU holds (Wait-CpuGate, before it launches the game): series-<pid>-<time>.json,
  {"pid", "since", "until", "cpus", "runs", "prefix"}. While it holds, the pool hands out
  `cpus` fewer slots (the VM's vCPUs; all of them for a host run), and the gate waits until
  that many are free. The hold ends when the run's events.json (in <runs>\\<prefix>*, a
  folder made after "since") says the Story world is ready, when the run has a result.json,
  at "until", or when the series' process is gone.
- RAM holds (Wait-RamGate, before it powers the VM on or uses it): ram-<pid>-<time>.json,
  {"pid", "since", "until", "cpus": 0, "vmx"}. While it holds, the pool reserves VM_RAM for
  that VM less its processes' working set now (vm_need: a VM that is up already holds most
  of it), so no new job is granted against it, and the gate (`jobpool vm-gate`) waits until
  the running jobs leave that much free. The series removes it as it ends, before it lets go
  of the VM; the pool ends it at "until" or when the series' process is gone (a series that
  was killed). A VM nobody holds reserves nothing: an idle VM that is up counts as the host's
  used RAM, one that is off as nothing.

    uv run dsff jobpool status        # slots, holders, holds, the RAM budget
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
_HELD: list = []  # this process's slots (Slot)
RAM_ENV = "DSFFORZA_POOL_RAM"  # "off": no RAM check (the tests' own pools, for their few small jobs)
SPARE_ENV = "DSFFORZA_POOL_SPARE"  # GB of RAM left to the owner's programs (default SPARE)
GB = 1 << 30
NL = chr(10)
# Left free for the owner's programs. A policy choice: the owner works on this PC while builds
# run. 3 GB is how far their use moved with no VM and no build running (24 to 27 GB available,
# 2026-11-14 05:18 to 05:32; research/in-game-tests.md, "Host cost").
SPARE = 3 * GB
# One test VM in game: vmware-vmx working set 7.0 to 7.2 GB plus mksSandbox 0.5 GB at 4 GB of guest
# RAM and 1 GB of graphics memory (research/in-game-tests.md, "Host cost"; both VMs together: 14.2 GB
# plus 0.7 GB). The guest RAM is in the vmx's working set within 80 s of power-on, plus 1.4 GB of
# overhead and the 3D part once the game renders ("Memory measured"): a RAM hold reserves this.
VM_RAM = int(7.7 * GB)
VM_IMAGES = ("vmware-vmx.exe", "mkssandbox.exe")  # the processes that hold a VM's RAM (lower case)


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
    """pool.lock, held while slots are counted and taken (a few milliseconds). Yields whether
    it got the lock: after `timeout` it goes on without (a holder that hangs; a slot more at
    worst), and a caller that must not (Slot.attach) checks."""
    fd = _open(folder / "pool.lock")
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while not (locked := _try_lock(fd)):
            if time.monotonic() > deadline:
                break
            time.sleep(0.02)
        yield locked
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
    """Slots without holds: a slot per logical CPU (the RAM budget bounds what they hold), at
    most gentle.CORES while out/gentle is on. With the RAM check off, port_batch.default_jobs
    (as many as WORKER_RAM each fits in the RAM). Every process must count the same slot
    files: older code sized the pool with a 1.9 GB WORKER_RAM, 16 slots here as now."""
    from dsfforza.build import gentle, port_batch

    n = port_batch.default_jobs(cpus, ram) if _ram_off() else max(1, cpus or os.cpu_count() or 2)
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
            reserved += int(hold.get("cpus", 0 if hold.get("vmx") else total))
        except (TypeError, ValueError):
            reserved += total
    return max(0, total - reserved)


# --- memory ----------------------------------------------------------------------------------


class Proc:
    """One process's memory (bytes), from SystemProcessInformation."""

    __slots__ = ("created", "name", "parent", "peak_private", "peak_ws", "pid", "private", "private_ws", "ws")

    def __init__(self, pid: int, parent: int, name: str, mem: tuple[int, int, int, int, int], created: int = 0):
        self.pid, self.parent, self.name, self.created = pid, parent, name, created
        self.ws, self.peak_ws, self.private, self.peak_private, self.private_ws = mem


def processes() -> dict[int, Proc]:
    """Every process's memory ({pid: Proc}; empty outside 64-bit Windows), from one call of
    NtQuerySystemInformation(SystemProcessInformation). It opens no process, so the elevated
    vmware-vmx counts too. The offsets are SYSTEM_PROCESS_INFORMATION's on x64, checked
    against Get-Process (WorkingSet64, PeakWorkingSet64, PrivateMemorySize64)."""
    if os.name != "nt" or sys.maxsize <= 1 << 32:
        return {}
    import ctypes
    import struct

    ntdll = ctypes.WinDLL("ntdll")
    size = ctypes.c_ulong(1 << 20)
    status, buf = 1, None
    for _ in range(8):
        buf = ctypes.create_string_buffer(size.value)
        status = ntdll.NtQuerySystemInformation(5, buf, size, ctypes.byref(size)) & 0xFFFFFFFF
        if status != 0xC0000004:  # STATUS_INFO_LENGTH_MISMATCH: more processes since; again, larger
            break
        size.value += 1 << 16
    if status != 0 or buf is None:
        return {}
    raw, base, out, off = buf.raw, ctypes.addressof(buf), {}, 0
    while off + 208 <= len(raw):
        nxt = struct.unpack_from("<I", raw, off)[0]
        private_ws = struct.unpack_from("<q", raw, off + 8)[0]  # WorkingSetPrivateSize
        created = struct.unpack_from("<q", raw, off + 32)[0]  # CreateTime (100 ns units)
        length, address = struct.unpack_from("<H", raw, off + 56)[0], struct.unpack_from("<Q", raw, off + 64)[0]
        pid, parent = struct.unpack_from("<QQ", raw, off + 80)
        peak_ws, ws = struct.unpack_from("<QQ", raw, off + 136)
        private, peak_private = struct.unpack_from("<QQ", raw, off + 184)  # PagefileUsage: private bytes
        name, start = "", address - base
        if address and 0 <= start <= len(raw) - length:
            name = raw[start : start + length].decode("utf-16-le", errors="replace")
        out[pid] = Proc(pid, parent, name, (ws, peak_ws, private, peak_private, private_ws), created)
        if not nxt:
            break
        off += nxt
    return out


def vm_pid(vmx: str, procs: dict[int, Proc]) -> int | None:
    """The running vmware-vmx process of a VM (None: it is off). Its vmware.log, next to the
    .vmx, starts "<UTC time> ... Log for VMware Workstation pid=<pid> ..." when the VM powers
    on; the pid counts while a vmware-vmx.exe of that pid started within a minute of that time
    (a later process may reuse the pid of a VM that is off)."""
    import datetime
    import re

    try:
        with (Path(vmx).parent / "vmware.log").open(encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except OSError:
        return None
    found = re.match(r"(\S+?)Z?\s.*Log for VMware Workstation pid=(\d+)", first)
    if not found:
        return None
    proc = procs.get(int(found.group(2)))
    if proc is None or proc.name.lower() != "vmware-vmx.exe":
        return None
    try:
        when = datetime.datetime.fromisoformat(found.group(1)).replace(tzinfo=datetime.UTC)
    except ValueError:
        return None
    epoch = datetime.datetime(1601, 1, 1, tzinfo=datetime.UTC)  # CreateTime: 100 ns units since 1601
    started = epoch + datetime.timedelta(microseconds=proc.created / 10)
    return proc.pid if abs((started - when).total_seconds()) < 60 else None


def vm_need(hold: dict, procs: dict[int, Proc]) -> int:
    """What a RAM hold's VM still needs (bytes): VM_RAM less the working set of its vmware-vmx
    and that process's children (its mksSandbox) now. The hold's "vm_pid", else vm_pid()."""
    pid = hold.get("vm_pid") or vm_pid(str(hold.get("vmx", "")), procs)
    return max(0, VM_RAM - sum(p.ws for p in tree(procs, pid)))


def ram_holds(folder: Path | None) -> list[dict]:
    """The active RAM holds (holds() with a "vmx"), one per VM (the earliest), oldest first."""
    seen, out = set(), []
    for hold in sorted(holds(folder) if folder is not None else [], key=lambda h: (h.get("since", 0), h["file"])):
        key = str(hold.get("vmx", "")).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(hold)
    return out


def spare() -> int:
    """Bytes of RAM left free for the owner's programs (SPARE, or SPARE_ENV's GB)."""
    try:
        return int(float(os.environ[SPARE_ENV]) * GB)
    except (KeyError, ValueError):
        return SPARE


def _slot_infos(folder: Path, total: int) -> list[dict]:
    """The busy slots' texts (who holds them, for what job)."""
    out = []
    for i in range(total):
        path = folder / f"slot-{i:02d}.lock"
        if not path.exists() or not _slot_busy(path):
            continue
        try:
            info = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            info = {}
        out.append({"slot": i, **info})
    return out


def budget(slots: list[dict], avail: int | None, procs: dict[int, Proc], vm_holds: list[dict], keep: int) -> dict:
    """The RAM a new slot may reserve now ("headroom", bytes; None when the host's available
    RAM is unknown) and its terms:

    - "growing": what the busy slots' jobs may still take: each slot's reservation ("ram")
      less the private working set of the process that holds the job's memory ("mem_pid",
      the holder's "pid" in the slot texts of older code); the whole reservation while that
      process isn't there (not started yet, or gone while its slot is still held);
    - "vms": what the VMs of `vm_holds` (ram_holds()) still need (vm_need);
    - "spare": `keep`, for the owner's programs."""
    growing = 0
    for s in slots:
        ram = int(s.get("ram") or 0)
        growing += max(0, ram - tree_private_ws(procs, _mem_pid(s)))
    vms = sum(vm_need(h, procs) for h in vm_holds)
    headroom = None if avail is None else avail - growing - vms - keep
    return {"available": avail, "growing": growing, "vms": vms, "spare": keep, "headroom": headroom}


def vm_gate(hold_file: str, folder: Path | None = None, free=None, procs=None) -> dict:
    """Whether a RAM hold's VM may start now: its need (vm_need) fits in the host's available
    RAM less what the busy jobs may still grow, SPARE, and what the VMs of earlier RAM holds
    still need. run-series.ps1's RAM gate asks this until it does ("ok")."""
    folder = folder if folder is not None else root()
    if folder is None or _ram_off():
        return {"ok": True, "need": 0, "free": None}
    with _pool_lock(folder):  # the slot texts as try_acquire reads them (Slot.attach writes under it)
        avail, table = _host(free, procs)
        mine = next((h for h in holds(folder) if h["file"] == hold_file), None)
        if mine is None or avail is None:
            return {"ok": True, "need": 0, "free": avail}
        # The earlier holds of the other VMs. An earlier hold of this VM (a series that ended
        # without removing it: one series uses a VM at a time) doesn't let this one pass
        # unchecked: the VM's need counts once, here.
        key, order = str(mine.get("vmx", "")).lower(), (mine.get("since", 0), mine["file"])
        earlier = [
            h
            for h in ram_holds(folder)
            if str(h.get("vmx", "")).lower() != key and (h.get("since", 0), h["file"]) < order
        ]
        slots = _slot_infos(folder, size())
    room = budget(slots, avail, table, earlier, spare())
    need = vm_need(mine, table)
    return {"ok": room["headroom"] >= need, "need": need, "free": room["headroom"], **room}


def tree(procs: dict[int, Proc], pid: int | None) -> list[Proc]:
    """A process and its descendants: a venv's python.exe is a launcher that runs the
    interpreter as its child, so a worker started as sys.executable holds its memory one
    level down."""
    if pid is None or pid not in procs:
        return []
    children: dict[int, list[int]] = {}
    for p in procs.values():
        children.setdefault(p.parent, []).append(p.pid)
    out, todo = [], [pid]
    while todo:
        p = procs.get(todo.pop())
        if p is None or p in out:
            continue
        out.append(p)
        # A child is younger than its parent: an older process naming this pid as its parent
        # was started by an earlier process of the same pid.
        todo.extend(c for c in children.get(p.pid, ()) if c != p.pid and procs[c].created >= p.created)
    return out


def tree_private_ws(procs: dict[int, Proc], pid: int | None) -> int:
    """The private working set of a process and its descendants (bytes; 0 when it is gone)."""
    return sum(p.private_ws for p in tree(procs, pid))


def _mem_pid(info: dict) -> int | None:
    """The process that holds a busy slot's job's memory (None: not started yet)."""
    pid = info["mem_pid"] if "mem_pid" in info else info.get("pid")
    try:
        return int(pid) if pid else None
    except (TypeError, ValueError):
        return None


def _ram_off() -> bool:
    return os.environ.get(RAM_ENV, "").strip().lower() in ("off", "0")


def _host(free=None, procs=None) -> tuple[int | None, dict[int, Proc]]:
    """The host's available RAM now (free(), default port_batch.free_ram) and its processes
    (procs(), default processes()). The processes are read first: memory a job takes between
    the two reads is then out of the available RAM and still in its slot's "growing", counted
    twice until the next grant, never credited twice (#619)."""
    if free is None:
        from dsfforza.build.port_batch import free_ram as free
    table = processes() if procs is None else procs()
    return free(), table


# --- slots -----------------------------------------------------------------------------------


class Slot:
    """One slot of the pool, held until release() or the process's end."""

    def __init__(self, path: Path | None, fd: int | None, label: str):
        self.path, self.fd, self.label = path, fd, label

    def attach(self, pid: int, timeout: float = 30.0) -> bool:
        """Name the process that holds this slot's job's memory, when it is a child the holder
        started (port_batch's conversion workers and lint processes): budget() counts that
        process's private working set against the slot's reservation. False: not named (no
        pool, or pool.lock not got within `timeout`), so the slot still counts all of it."""
        if self.fd is None or self.path is None:
            return False
        # Under pool.lock, as every reader that counts the slot's reservation (try_acquire,
        # vm_gate): none of them sees the file half written. Without the lock (a holder that
        # hangs) it isn't written at all: the slot keeps "mem_pid": null, its whole reservation.
        # It is written over in place, padded to its old length, and cut only after, so it
        # never holds less than a whole text.
        with contextlib.suppress(OSError, ValueError), _pool_lock(self.path.parent, timeout) as locked:
            if not locked:
                return False
            os.lseek(self.fd, 0, os.SEEK_SET)
            old = os.read(self.fd, 1 << 16)
            info = json.loads(old.decode() or "{}")
            info["mem_pid"] = int(pid)
            text = json.dumps(info).encode()
            os.lseek(self.fd, 0, os.SEEK_SET)
            os.write(self.fd, text.ljust(len(old)))
            os.ftruncate(self.fd, len(text))
            return True
        return False

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
_TICKET: list = []  # [fd, path, since, ram] while this process waits


def _ticket_text(since: float, ram: int) -> bytes:
    return json.dumps({"pid": os.getpid(), "since": since, "ram": ram}).encode()


def _join_queue(folder: Path, ram: int = 0) -> None:
    if _TICKET:
        if _TICKET[3] != ram:  # this process waits for a job of another kind now
            fd, _, since, _ = _TICKET
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                os.ftruncate(fd, 0)
                os.write(fd, _ticket_text(since, ram))
            _TICKET[3] = ram
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
        os.write(fd, _ticket_text(since, ram))
    _TICKET.extend((fd, path, since, ram))


def leave_queue() -> None:
    """Stop waiting in line (try_acquire does when it gets a slot)."""
    if not _TICKET:
        return
    fd, path, _, _ = _TICKET
    _TICKET.clear()
    _unlock(fd)
    with contextlib.suppress(OSError):
        os.close(fd)
    with contextlib.suppress(OSError):
        path.unlink()


def _ahead(folder: Path) -> tuple[int, int]:
    """Waiters of other processes that joined the queue before this one (before now, when
    this one doesn't wait), and the RAM their jobs want: a later, smaller job doesn't take
    the RAM an earlier, larger one waits for."""
    mine = _TICKET[2] if _TICKET else time.time()
    own = _TICKET[1] if _TICKET else None
    n = ram = 0
    try:
        tickets = list((folder / "queue").glob("*.lock"))
    except OSError:
        return 0, 0
    for path in tickets:
        if path == own or not _slot_busy(path):  # not waiting any more (its process ended)
            continue
        try:
            ticket = json.loads(path.read_text(encoding="utf-8") or "{}")
            since, wants = float(ticket.get("since", 0)), int(ticket.get("ram") or 0)
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if since < mine:
            n += 1
            ram += wants
    return n, ram


def try_acquire(
    label: str,
    ram: int = 0,
    free=None,
    folder: Path | None = None,
    total: int | None = None,
    *,
    queue: bool = True,
    child: bool = False,
    procs=None,
    vm_holds: list[dict] | None = None,
) -> Slot | None:
    """A slot for a job whose measured peak is `ram` bytes, or None: the pool is at capacity
    (or holds reserve it), the free slots go to processes that have waited longer, or the
    RAM budget has less than `ram` (budget(): the host's available RAM less what the busy
    slots may still grow, the VMs' unmet need, SPARE, and what earlier waiters want). The
    slot reserves `ram` while it is held. The job's memory is this process's, or with
    `child`, a process it starts next and names with Slot.attach (until then the slot counts
    its whole reservation). free(): bytes available now (default port_batch.free_ram);
    procs(): processes() (default); vm_holds: ram_holds() (default). Without a slot the process
    joins the queue (unless `queue` is False: it will not ask again soon). With the pool
    off, always a slot (that holds nothing)."""
    folder = folder if folder is not None else root()
    if folder is None:
        return Slot(None, None, label)
    total = size() if total is None else total
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with _pool_lock(folder):
            cap = capacity(folder, total)
            paths = [folder / f"slot-{i:02d}.lock" for i in range(total)]
            busy = [p for p in paths if _slot_busy(p)]
            waiting, wanted = _ahead(folder)
            fits = True
            if ram and not _ram_off():
                avail, table = _host(free, procs)
                held_vms = ram_holds(folder) if vm_holds is None else vm_holds
                room = budget(_slot_infos(folder, total), avail, table, held_vms, spare())
                fits = room["headroom"] is None or room["headroom"] - wanted >= ram
            if cap - len(busy) <= waiting or not fits:
                if queue:
                    _join_queue(folder, ram)
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
                    "mem_pid": None if child else os.getpid(),
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
            log(f"job pool: waiting for a slot ({label}, {ram / GB:.1f} GB; uv run dsff jobpool status)")
            said = True
        time.sleep(poll)


def lease(label: str, want: int, ram: int = 0, log=None, *, child: bool = False) -> list[Slot]:
    """Up to `want` slots: one waited for, the others as free now (for a step that starts
    its processes at once, like port_batch.lint_all; `child`: as try_acquire)."""
    first = acquire(label, ram, log=log, child=child)
    out = [first]
    while len(out) < want:
        slot = try_acquire(label, ram, queue=False, child=child)
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


def on_slot(label: str, ram: int, fn, *args):
    """fn(*args) on a pool slot taken for it (waited for) and freed when it returns: for a
    process that runs one task, like ProcessPoolExecutor(max_tasks_per_child=1)'s. Such an
    executor starts a process after each one ends, the last one's too (CPython 3.13
    concurrent.futures.process, _ExecutorManagerThread.run -> _adjust_process_count), and
    shutdown waits for it: a process that took its slot in its initializer could wait there
    for a slot (a RAM hold) with no task left, and the executor with it (#619). Use as
    functools.partial(jobpool.on_slot, label, ram, fn)."""
    with acquire(label, ram):
        return fn(*args)


def executor(jobs: int, label: str, ram: int = 0, initializer=None, initargs: tuple = (), **kwargs):
    """A ProcessPoolExecutor of up to `jobs` processes, each holding a pool slot for its
    life: a process waits for its slot before it takes work, and its slot goes with it.
    Not with max_tasks_per_child (on_slot instead)."""
    if kwargs.get("max_tasks_per_child"):
        raise ValueError("jobpool.executor: a slot per process; with max_tasks_per_child use on_slot")
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


def adopt(slot: Slot, proc) -> None:
    """A child process (subprocess.Popen) a slot was taken for (try_acquire(child=True)): it
    ends with this process (die_with_me), and the pool counts its memory against the slot
    (Slot.attach)."""
    die_with_me(proc)
    slot.attach(proc.pid)


# --- status ------------------------------------------------------------------------------------


def status(folder: Path | None = None, total: int | None = None) -> dict:
    folder = folder if folder is not None else root()
    if folder is None:
        return {"pool": None}
    total = size() if total is None else total
    slots = _slot_infos(folder, total)
    avail, procs = _host()
    for s in slots:
        members = tree(procs, _mem_pid(s))
        if members:
            s.update({k: sum(getattr(p, k) for p in members) for k in ("private_ws", "peak_private", "peak_ws")})
    waiting, wanted = _ahead(folder)
    return {
        "pool": str(folder),
        "size": total,
        "capacity": capacity(folder, total),
        "busy": slots,
        "holds": holds(folder),
        "ram": None if _ram_off() else budget(slots, avail, procs, ram_holds(folder), spare()),
        "waiting": waiting,
        "waiting_ram": wanted,
    }


def _kind(label: str) -> str:
    """A slot label's job kind: a conversion worker's label is its car."""
    known = ("lint", "garage pictures", "render_compare", "memo", "cockpit_survey", "plates_survey", "gui_assets")
    return label if label.startswith(known) else "conversion worker"


def watch(out: Path, every: float = 2.0, seconds: float = 0.0, folder: Path | None = None) -> dict:
    """Sample the host's memory and the pool every `every` s (until `seconds`, or Ctrl+C):
    out (.csv) gets a row per sample, out with .peaks.json each job process's peaks (the
    kernel's peak working set and peak private bytes of each busy slot's job process, the
    last seen before it ended: growth in its last `every` s can be missed). For the
    measurements of research/batch.md, "Job pool", "RAM"."""
    from dsfforza.build.port_batch import _memory_status

    folder = folder if folder is not None else root()
    total = size()
    peaks: dict[tuple, dict] = {}
    low = None
    out.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    with out.open("w", encoding="utf-8") as f:
        f.write("time,available_gb,commit_gb,headroom_gb,growing_gb,vms_short_gb,vm_ws_gb,busy,waiting,kinds" + NL)
        try:
            while not seconds or time.time() - t0 < seconds:
                slots = _slot_infos(folder, total) if folder is not None else []
                avail, procs = _host()
                room = budget(slots, avail, procs, ram_holds(folder), spare())
                kinds: dict[str, int] = {}
                for s in slots:
                    kind = _kind(str(s.get("label", "")))
                    kinds[kind] = kinds.get(kind, 0) + 1
                    pid = _mem_pid(s)
                    members = tree(procs, pid)
                    if not members:
                        continue
                    seen = peaks.setdefault(
                        (pid, s.get("label")), {"pid": pid, "label": s.get("label"), "kind": kind, "cwd": s.get("cwd")}
                    )
                    # The job's figures: the sums over its process tree (a launcher and its interpreter).
                    for key in ("peak_ws", "peak_private", "private_ws"):
                        seen[key] = max(seen.get(key, 0), sum(getattr(p, key) for p in members))
                vm_ws = sum(p.ws for p in procs.values() if p.name.lower() in VM_IMAGES)
                commit = (_memory_status("page_total") or 0) - (_memory_status("page_avail") or 0)  # commit charge
                if avail is not None:
                    low = avail if low is None else min(low, avail)
                waiting = _ahead(folder)[0] if folder is not None else 0

                def gb(v):
                    return "" if v is None else f"{v / GB:.2f}"

                f.write(
                    f"{time.strftime('%H:%M:%S')},{gb(avail)},{gb(commit)},{gb(room['headroom'])},"
                    f"{gb(room['growing'])},{gb(room['vms'])},{gb(vm_ws)},{len(slots)},{waiting},"
                    + " ".join(f"{k}={n}" for k, n in sorted(kinds.items()))
                    + NL
                )
                f.flush()
                out.with_suffix(".peaks.json").write_text(
                    json.dumps({"lowest_available": low, "processes": list(peaks.values())}, indent=1),
                    encoding="utf-8",
                )
                time.sleep(every)
        except KeyboardInterrupt:
            pass
    return {"lowest_available": low, "processes": list(peaks.values())}


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="dsff jobpool", description=__doc__.split(NL + NL)[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status", help="slots, their holders, the RAM budget and the test runs' holds")
    s.add_argument("--json", action="store_true")
    w = sub.add_parser("watch", help="log the host's memory and each job process's peaks (a .csv and .peaks.json)")
    w.add_argument("--out", type=Path, required=True, help="the .csv")
    w.add_argument("--every", type=float, default=2.0, help="seconds between samples")
    w.add_argument("--seconds", type=float, default=0.0, help="stop after (default: Ctrl+C)")
    g = sub.add_parser("vm-gate", help="exit 0 when a RAM hold's VM fits in the RAM now, 1 while it doesn't")
    g.add_argument("--hold", required=True, help=r"the hold's file name in holds\ (ram-<pid>-<time>.json)")
    args = parser.parse_args(argv)
    if args.cmd == "vm-gate":
        gate = vm_gate(args.hold)
        free = gate.get("free")
        print(
            f"VM needs {gate['need'] / GB:.1f} GB, {'none known' if free is None else f'{free / GB:.1f} GB'} free for it"
            + (f" ({gate['growing'] / GB:.1f} GB the running jobs may still take)" if gate.get("growing") else "")
        )
        sys.exit(0 if gate["ok"] else 1)
    if args.cmd == "watch":
        found = watch(args.out, args.every, args.seconds)
        low = found["lowest_available"]
        print(f"{len(found['processes'])} job processes; lowest available {low / GB if low else 0:.2f} GB")
        return
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
        mem = f", {s_['private_ws'] / GB:.1f} of {int(s_.get('ram') or 0) / GB:.1f} GB" if "private_ws" in s_ else ""
        print(
            f"  slot {s_['slot']:2d}: pid {s_.get('pid')} {s_.get('label', '')} ({age:.0f} s{mem}) {s_.get('cwd', '')}"
        )
    for h in st["holds"]:
        if h.get("vmx"):
            print(f"  hold {h['file']}: RAM of {h['vmx']} for pid {h.get('pid')} while its series runs")
            continue
        print(
            f"  hold {h['file']}: {h.get('cpus')} CPUs for pid {h.get('pid')} until the world loads ({h.get('prefix')})"
        )
    ram = st["ram"]
    if ram and ram["available"] is not None:
        print(
            f"  RAM: {ram['available'] / GB:.1f} GB available - {ram['growing'] / GB:.1f} the busy jobs may still take"
            f" - {ram['vms'] / GB:.1f} the test VMs still need - {ram['spare'] / GB:.1f} spare"
            f" = {ram['headroom'] / GB:.1f} GB for new jobs"
            + (f"; {st['waiting']} waiting for {st['waiting_ram'] / GB:.1f} GB" if st["waiting"] else "")
        )


if __name__ == "__main__":
    main()
