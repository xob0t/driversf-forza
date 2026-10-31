"""tools/re/forza_recomp.py: the isolation's job setup refuses to launch when a call fails, fh1-run
fails on a failed runner, and the constant filter keeps named constants (research/forza-recomp.md)."""

import importlib.util
import tempfile
import types
import unittest
from pathlib import Path

from dsfforza import REPO

_spec = importlib.util.spec_from_file_location("forza_recomp", REPO / "tools" / "re" / "forza_recomp.py")
fr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fr)


def fake_k32(**results):
    """A kernel32 stand-in: each call returns `results[name]` (default success) and is logged."""
    calls = []
    defaults = {
        "CreateJobObjectW": 0x10,
        "SetInformationJobObject": 1,
        "AssignProcessToJobObject": 1,
        "ResumeThread": 1,
        "TerminateProcess": 1,
        "TerminateJobObject": 1,
        "CloseHandle": 1,
    }
    k32 = types.SimpleNamespace(calls=calls)
    for name, default in defaults.items():
        result = results.get(name, default)

        def call(*args, _name=name, _result=result):
            calls.append((_name, [getattr(a, "value", a) for a in args]))
            return _result

        setattr(k32, name, call)  # plain functions: make_job sets .restype on them
    return k32


def names(k32):
    return [name for name, _ in k32.calls]


class JobSetup(unittest.TestCase):
    def test_job_has_kill_on_close(self):
        k32 = fake_k32()
        self.assertEqual(fr.make_job(k32), 0x10)
        self.assertEqual(names(k32), ["CreateJobObjectW", "SetInformationJobObject"])

    def test_no_job_no_launch(self):
        with self.assertRaises(OSError):
            fr.make_job(fake_k32(CreateJobObjectW=0))

    def test_kill_on_close_refused_closes_the_job(self):
        k32 = fake_k32(SetInformationJobObject=0)
        with self.assertRaises(OSError):
            fr.make_job(k32)
        self.assertIn(("CloseHandle", [0x10]), k32.calls)

    def test_joins_then_runs(self):
        k32 = fake_k32()
        fr.join_job_and_resume(k32, 0x10, 0x20, 0x30)
        self.assertEqual(names(k32), ["AssignProcessToJobObject", "ResumeThread", "CloseHandle"])
        self.assertNotIn("TerminateProcess", names(k32))

    def test_a_process_outside_the_job_never_runs(self):
        k32 = fake_k32(AssignProcessToJobObject=0)
        with self.assertRaises(OSError):
            fr.join_job_and_resume(k32, 0x10, 0x20, 0x30)
        self.assertIn(("TerminateProcess", [0x20, 1]), k32.calls)
        self.assertNotIn("ResumeThread", names(k32))
        self.assertIn(("CloseHandle", [0x20]), k32.calls)
        self.assertIn(("CloseHandle", [0x30]), k32.calls)

    def test_failed_resume_ends_the_job(self):
        k32 = fake_k32(ResumeThread=fr.RESUME_FAILED)
        with self.assertRaises(OSError):
            fr.join_job_and_resume(k32, 0x10, 0x20, 0x30)
        self.assertIn(("TerminateJobObject", [0x10, 1]), k32.calls)


class FakeIsolated:
    """Ends at once with `code`; writes a frame dump just before it ends."""

    code = 0

    def __init__(self, argv, _env, _cwd):
        prefix = "--game-argument=--fh1_frame_dump_path="
        dump = next(a[len(prefix) :] for a in argv if a.startswith(prefix))
        Path(dump).write_bytes(b"FH1FRM01")
        self.pid, self.breached, self.stopped = 1, False, False

    def wait(self, _timeout):
        return True

    def exit_code(self):
        return self.code

    def stop(self):
        self.stopped = True


class Fh1Run(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "pinyon" / "config" / "render-tests").mkdir(parents=True)
        (root / "pinyon" / "config" / "render-tests" / "fh1-opening-sync.fh1test").write_text(
            "pinyon-shift-fh1-render-test-v1\ninput 0 0000 0 0 0 0 0 0\ncapture 10 x\nstop 20\n"
        )
        self.steps = root / "steps"
        self.steps.write_text("# ours\ncapture 30 day\nstop 40\n")
        self.run_dir = root / "run"
        self.saved = fr.PINYON, fr.Isolated
        fr.PINYON, fr.Isolated = root / "pinyon", FakeIsolated

    def tearDown(self):
        fr.PINYON, fr.Isolated = self.saved
        self.tmp.cleanup()

    def test_failed_runner_fails(self):
        FakeIsolated.code = 1
        with self.assertRaises(RuntimeError):
            fr.fh1_run(self.steps, self.run_dir, [])

    def test_route_and_last_dump(self):
        FakeIsolated.code = 0
        fr.fh1_run(self.steps, self.run_dir, [])
        route = (self.run_dir / "route.fh1test").read_text().splitlines()
        self.assertEqual(
            route, ["pinyon-shift-fh1-render-test-v1", "input 0 0000 0 0 0 0 0 0", "capture 30 day", "stop 40"]
        )
        self.assertTrue((self.run_dir / "frames" / "0.fh1frame").exists())  # written just before the end


def test_named_constants_kept():
    pc = {"c_eyePosWS@c59": [1.0], "cubeScale@c88": [0.9], "c12@c12": [0.0], "BrakeColor@c89": [4.95]}
    assert fr.named_only(pc) == {"c_eyePosWS@c59": [1.0], "cubeScale@c88": [0.9], "BrakeColor@c89": [4.95]}


if __name__ == "__main__":
    unittest.main()
