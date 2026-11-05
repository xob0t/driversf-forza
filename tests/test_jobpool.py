"""The machine-wide job pool (src/dsfforza/build/jobpool.py): slots across processes, freed
when their process dies, capped by holds while a test run's game loads.

uv run python -m tests jobpool
"""

from __future__ import annotations

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


def test_a_second_slot_waits_for_ram():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        first = jobpool.try_acquire("a", ram=1 << 40, free=lambda: 1 << 20, folder=folder, total=4)
        assert first is not None  # a process holding none gets one whatever the RAM
        assert jobpool.try_acquire("b", ram=1 << 40, free=lambda: 1 << 20, folder=folder, total=4, queue=False) is None
        second = jobpool.try_acquire("b", ram=1 << 20, free=lambda: 1 << 30, folder=folder, total=4)
        assert second is not None
        first.release()
        second.release()


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
