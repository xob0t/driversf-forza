"""The machine-wide job pool (src/dsfforza/build/jobpool.py): slots across processes, freed
when their process dies, capped by holds while a test run's game loads.

uv run python -m tests jobpool
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dsfforza.build import fingerprint, jobpool

HOLDER = """
import sys, time
from pathlib import Path
from dsfforza.build import jobpool
slots = [jobpool.try_acquire("child", folder=Path(sys.argv[1]), total=int(sys.argv[2])) for _ in range(int(sys.argv[3]))]
print(sum(s is not None for s in slots), flush=True)
time.sleep(60)
"""


def _child(folder: Path, total: int, count: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(folder), str(total), str(count)], stdout=subprocess.PIPE, text=True
    )
    assert proc.stdout.readline().strip() == str(count)
    return proc


def test_slots_are_shared_and_freed_when_the_holder_dies():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        child = _child(folder, 4, 3)  # another process holds 3 of 4
        again = None
        try:
            mine = jobpool.try_acquire("me", folder=folder, total=4)
            assert mine is not None
            assert jobpool.try_acquire("me", folder=folder, total=4, queue=False) is None  # all 4 busy
            assert {s["label"] for s in jobpool.status(folder, 4)["busy"]} == {"child", "me"}
            mine.release()
            again = jobpool.try_acquire("me", folder=folder, total=4)
            assert again is not None
        finally:
            child.kill()
            child.wait()
        # The child's 3 slots went with it (Windows drops a dead process's locks soon after
        # it ends, "depending on available system resources": LockFileEx).
        more, deadline = [], time.monotonic() + 30
        while len(more) < 3 and time.monotonic() < deadline:
            slot = jobpool.try_acquire("me", folder=folder, total=4)
            if slot is None:
                time.sleep(0.1)
            else:
                more.append(slot)
        assert len(more) == 3
        for s in [again, *more]:
            s.release()
        assert jobpool.held() == 0


WAITER = """
import sys
from pathlib import Path
from dsfforza.build import jobpool
print("waiting", flush=True)
slot = jobpool.acquire("waiter", folder=Path(sys.argv[1]), total=1, poll=0.05)
print("got it", flush=True)
sys.stdin.readline()
"""


def test_a_freed_slot_goes_to_who_waited_longest():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        mine = jobpool.try_acquire("me", folder=folder, total=1)
        waiter = subprocess.Popen(
            [sys.executable, "-c", WAITER, str(folder)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
        )
        try:
            assert waiter.stdout.readline().strip() == "waiting"
            deadline = time.monotonic() + 30
            while not list((folder / "queue").glob("*.lock")) and time.monotonic() < deadline:
                time.sleep(0.05)
            mine.release()
            assert jobpool.try_acquire("me", folder=folder, total=1) is None  # the waiter is first
            assert waiter.stdout.readline().strip() == "got it"
            assert jobpool.try_acquire("me", folder=folder, total=1) is None  # it holds the slot
        finally:
            waiter.stdin.close()
            waiter.wait()
        jobpool.leave_queue()


GB = jobpool.GB


def _proc(pid: int, private_ws: int, parent: int = 0, name: str = "python.exe", created: int = 0, ws: int = 0):
    return jobpool.Proc(pid, parent, name, (ws, 0, 0, 0, private_ws), created)


@contextlib.contextmanager
def _ram_check():
    """The RAM budget on (the test run turns it off for its own builds: tests/__main__.py)."""
    saved = os.environ.pop(jobpool.RAM_ENV, None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ[jobpool.RAM_ENV] = saved


def test_every_slot_waits_for_ram_the_first_of_a_process_too():
    with _ram_check(), tempfile.TemporaryDirectory() as tmp:
        kw = {"folder": Path(tmp), "total": 4, "vms": 0, "procs": dict}
        assert jobpool.held() == 0  # this process holds none: no exception for it (#613)
        # 4 GB available less 3 GB spare: 1 GB for jobs.
        assert jobpool.try_acquire("a", ram=2 * GB, free=lambda: 4 * GB, queue=False, **kw) is None
        first = jobpool.try_acquire("a", ram=2 * GB, free=lambda: 6 * GB, **kw)
        assert first is not None
        # The first job reserves its 2 GB before it uses any: 6 - 2 - 3 = 1 GB left.
        assert jobpool.try_acquire("b", ram=2 * GB, free=lambda: 6 * GB, queue=False, **kw) is None
        second = jobpool.try_acquire("b", ram=1 * GB, free=lambda: 6 * GB, **kw)
        assert second is not None
        first.release()
        second.release()


def test_a_slot_counts_what_its_job_holds_once():
    with _ram_check(), tempfile.TemporaryDirectory() as tmp:
        folder, me = Path(tmp), os.getpid()
        slot = jobpool.try_acquire("a", ram=4 * GB, free=lambda: 10 * GB, folder=folder, total=4, vms=0, procs=dict)
        infos = jobpool._slot_infos(folder, 4)
        # The job holds 3 of its 4 GB, which the host's 7 GB available already leave out.
        room = jobpool.budget(infos, 7 * GB, {me: _proc(me, 3 * GB)}, vms=0, keep=3 * GB)
        assert room["growing"] == 1 * GB and room["headroom"] == 3 * GB
        # Past its reservation it adds nothing more.
        room = jobpool.budget(infos, 5 * GB, {me: _proc(me, 5 * GB)}, vms=0, keep=3 * GB)
        assert room["growing"] == 0 and room["headroom"] == 2 * GB
        slot.release()


def test_a_child_job_reserves_all_until_attached_then_counts_its_process_tree():
    with _ram_check(), tempfile.TemporaryDirectory() as tmp:
        folder, me = Path(tmp), os.getpid()
        kw = {"folder": folder, "total": 4, "vms": 0, "procs": dict, "free": lambda: 10 * GB}
        slot = jobpool.try_acquire("car", ram=2 * GB, child=True, **kw)
        assert jobpool._slot_infos(folder, 4)[0]["mem_pid"] is None
        # The build process is large: none of it counts for its child's slot.
        assert (
            jobpool.budget(jobpool._slot_infos(folder, 4), 10 * GB, {me: _proc(me, 5 * GB)}, 0, 0)["growing"] == 2 * GB
        )
        slot.attach(900)
        info = jobpool._slot_infos(folder, 4)[0]
        assert info["mem_pid"] == 900 and info["label"] == "car" and info["ram"] == 2 * GB
        # 900 is a venv launcher whose interpreter, 901, holds the memory. 902 is older than 900:
        # an earlier process of that pid started it, so it isn't counted.
        procs = {
            900: _proc(900, 10 << 20, parent=me, created=100),
            901: _proc(901, int(1.5 * GB), parent=900, created=101),
            902: _proc(902, 1 * GB, parent=900, created=50),
        }
        assert jobpool.tree_private_ws(procs, 900) == int(1.5 * GB) + (10 << 20)
        assert jobpool.budget([info], 10 * GB, procs, 0, 0)["growing"] == 2 * GB - int(1.5 * GB) - (10 << 20)
        slot.release()


def test_the_vms_unmet_need_is_reserved():
    vmx, mks = int(7.2 * GB), int(0.5 * GB)
    one_vm = {1: _proc(1, 0, name="vmware-vmx.exe", ws=vmx), 2: _proc(2, 0, name="mksSandbox.exe", ws=mks)}
    off = jobpool.budget([], 20 * GB, {}, vms=2, keep=3 * GB)
    assert off["vms"] == 2 * jobpool.VM_RAM and off["headroom"] == 20 * GB - 2 * jobpool.VM_RAM - 3 * GB
    assert jobpool.budget([], 13 * GB, one_vm, vms=2, keep=3 * GB)["vms"] == 2 * jobpool.VM_RAM - vmx - mks
    assert jobpool.budget([], 13 * GB, one_vm, vms=1, keep=3 * GB)["vms"] == 0  # in game: it holds its need


def test_concurrent_jobs_keep_the_floor():
    """Jobs of three sizes ask, start, grow to their peaks and end: the host's available RAM
    stays above the floor (SPARE plus a VM's need) throughout, with the owner's 8 GB used."""
    with _ram_check(), tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        holding: dict[int, int] = {}  # fake pid -> bytes it holds now

        def avail():
            return 32 * GB - 8 * GB - sum(holding.values())

        def procs():
            return {pid: _proc(pid, held) for pid, held in holding.items()}

        running, granted, refused = [], 0, 0
        for step in range(90):
            ram = (4 * GB, int(1.9 * GB), int(2.6 * GB))[step % 3]
            kw = {"free": avail, "procs": procs, "folder": folder, "total": 16, "vms": 1, "child": True}
            slot = jobpool.try_acquire(f"job{step}", ram, queue=False, **kw)
            if slot is None:
                refused += 1
            else:
                granted += 1
                pid = 1000 + step
                slot.attach(pid)
                holding[pid] = 0
                running.append((slot, pid, ram))
            holding.update({pid: peak for _, pid, peak in running})  # each running job grows to its peak
            assert avail() >= jobpool.SPARE + jobpool.VM_RAM, step
            if step % 5 == 4 and running:  # one ends now and then
                slot, pid, _ = running.pop(0)
                holding.pop(pid)
                slot.release()
        assert granted >= 10 and refused >= 10
        for slot, _, _ in running:
            slot.release()


TICKET = """
import sys, time
from pathlib import Path
from dsfforza.build import jobpool
jobpool._join_queue(Path(sys.argv[1]), 4 << 30)
print("queued", flush=True)
time.sleep(60)
"""


def test_a_later_smaller_job_leaves_the_ram_an_earlier_waiter_wants():
    with _ram_check(), tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "queue").mkdir()
        waiter = subprocess.Popen([sys.executable, "-c", TICKET, str(folder)], stdout=subprocess.PIPE, text=True)
        try:
            assert waiter.stdout.readline().strip() == "queued"
            kw = {"folder": folder, "total": 4, "vms": 0, "procs": dict, "queue": False, "free": lambda: 9 * GB}
            # 9 GB available less 3 spare: 6 GB, of which the earlier waiter wants 4.
            assert jobpool.try_acquire("small", ram=3 * GB, **kw) is None
            small = jobpool.try_acquire("small", ram=2 * GB, **kw)
            assert small is not None
            small.release()
        finally:
            waiter.kill()
            waiter.wait()


def test_processes_reads_this_process():
    if os.name != "nt":
        return
    me = jobpool.processes()[os.getpid()]
    assert me.name.lower().startswith("python") and me.private_ws > 0 and me.peak_ws >= me.ws > 0


def _hold(folder: Path, **values) -> Path:
    (folder / "holds").mkdir(parents=True, exist_ok=True)
    now = time.time()
    hold = {"pid": os.getpid(), "since": now, "until": now + 600, "cpus": 3, **values}
    path = folder / "holds" / f"series-{hold['pid']}-{int(now)}.json"
    path.write_text(json.dumps(hold), encoding="utf-8")
    return path


def test_a_hold_reserves_cpus_until_the_world_loads():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "pool"
        runs = Path(tmp) / "runs"
        runs.mkdir()
        hold = _hold(folder, runs=str(runs), prefix="vm2-")
        assert jobpool.capacity(folder, 4) == 1
        slot = jobpool.try_acquire("a", folder=folder, total=4)
        assert slot is not None and jobpool.try_acquire("b", folder=folder, total=4, queue=False) is None
        # Another VM's run folder, or an older one of this VM, doesn't end it.
        (runs / "vm1-x").mkdir()
        (runs / "vm1-x" / "events.json").write_text('[{"message": "story world ready after 3 presses"}]')
        assert jobpool.capacity(folder, 4) == 1
        run = runs / "vm2-20261105-120000"
        run.mkdir()
        (run / "events.json").write_text('[{"message": "launch"}]')
        assert jobpool.capacity(folder, 4) == 1
        (run / "events.json").write_text('[{"message": "Story world ready after 14 presses"}]')
        assert jobpool.capacity(folder, 4) == 4 and not hold.exists()
        slot.release()


def test_a_hold_ends_with_its_process_or_its_time():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        _hold(folder, pid=proc.pid, cpus=4)
        assert jobpool.capacity(folder, 4) == 4
        _hold(folder, until=time.time() - 1, cpus=4)
        assert jobpool.capacity(folder, 4) == 4
        _hold(folder, cpus=99)  # a host run: every slot
        assert jobpool.capacity(folder, 4) == 0


def test_executor_processes_take_slots():
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get(jobpool.POOL_ENV)
        os.environ[jobpool.POOL_ENV] = tmp
        try:
            with jobpool.executor(2, "test") as pool:
                held = [f.result() for f in [pool.submit(jobpool.held) for _ in range(6)]]
            assert held == [1] * 6  # each process on one slot of its own
        finally:
            if saved is None:
                os.environ.pop(jobpool.POOL_ENV, None)
            else:
                os.environ[jobpool.POOL_ENV] = saved


def test_the_pool_is_no_input_of_a_fingerprint():
    with tempfile.TemporaryDirectory() as tmp:
        saved = os.environ.get(jobpool.POOL_ENV)
        os.environ[jobpool.POOL_ENV] = tmp
        try:
            rec = fingerprint.Recorder().start()
            _hold(Path(tmp), cpus=1)
            jobpool.capacity()
            slot = jobpool.try_acquire("fp")
            slot.release()
            fp = rec.finish({"media": "X"})
        finally:
            if saved is None:
                os.environ.pop(jobpool.POOL_ENV, None)
            else:
                os.environ[jobpool.POOL_ENV] = saved
        assert "dsfforza.build.jobpool" not in fp["code"]
        assert not [p for p in [*fp["files"], *fp["dirs"]] if p.startswith(fingerprint.norm(tmp))]


def test_powershell_sees_a_held_slot():
    if os.name != "nt":
        return
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        slot = jobpool.try_acquire("ps", folder=folder, total=2)
        script = (
            f"$busy = 0; foreach ($f in Get-ChildItem -LiteralPath '{folder}' -Filter 'slot-*.lock') {{"
            " $fs = [IO.File]::Open($f.FullName, 'Open', 'ReadWrite', 'ReadWrite');"
            " try { $fs.Lock(1GB, 1); $fs.Unlock(1GB, 1) } catch { $busy++ } finally { $fs.Dispose() } }; $busy"
        )
        run = ["powershell", "-NoProfile", "-Command", script]
        assert subprocess.run(run, capture_output=True, text=True, check=True).stdout.strip() == "1"
        slot.release()
        assert subprocess.run(run, capture_output=True, text=True, check=True).stdout.strip() == "0"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
