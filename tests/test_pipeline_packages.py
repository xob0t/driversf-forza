"""port_batch --packages (write_packages) and install.py --packages, without a real build."""

import argparse
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dsfforza.build import dsfcar, install, port_batch


class WritePackagesTest(unittest.TestCase):
    def setUp(self):
        self.out = Path(tempfile.mkdtemp())
        (self.out / port_batch.PACKAGES).mkdir()
        self.args = argparse.Namespace(iso=None, packages=True)
        self.events = []

    def progress(self, event, **data):
        self.events.append((event, data))

    def test_writes_and_drops_old_packages(self):
        folder = self.out / port_batch.PACKAGES
        stale = folder / "forza.fh1.GONE_10.dsfcar"
        stale.write_bytes(b"old")
        keep = folder / "forza.fh1.CAR_A_10.dsfcar"

        def fake_pack(build, media, out_dir, isos=None):
            self.assertEqual((build, media, out_dir), (self.out, ["CAR_A_10"], folder))
            keep.write_bytes(b"new")
            return [keep]

        timings = {}
        with mock.patch.object(dsfcar, "pack", side_effect=fake_pack):
            port_batch.write_packages(self.args, self.out, [{"media": "CAR_A_10"}], timings, self.progress)
        self.assertFalse(stale.exists())
        self.assertTrue(keep.exists())
        self.assertEqual(timings["packages"], 1)
        self.assertEqual(self.events[0], ("packages", {"cars": 1}))

    def test_a_failure_is_reported_not_raised(self):
        timings = {}
        with mock.patch.object(dsfcar, "pack", side_effect=ValueError("no VO3D")):
            port_batch.write_packages(self.args, self.out, [{"media": "CAR_A_10"}], timings, self.progress)
        self.assertIn("no VO3D", timings["packages_error"])
        self.assertEqual(self.events[-1][0], "packages_failed")


class InstallFlagsTest(unittest.TestCase):
    def test_packages_flag_reaches_install_ps1(self):
        run = install.Run.__new__(install.Run)
        run.args = argparse.Namespace(skip_hook=False, packages=True, no_write_profile=False, force=False)
        run.plugin = Path("out/asi/dsfforza.asi")
        run.record = None
        flags = run.install_flags(None, None)
        self.assertIn("-Packages", flags)
        self.assertEqual(flags[:3], ["-Asi", "-AsiPath", str(run.plugin)])
        run.args.packages = False
        self.assertNotIn("-Packages", run.install_flags(None, None))

    def test_tracker_shows_the_packages_step(self):
        tracker = install.Tracker()
        notes = tracker.handle("packages", cars=3)
        self.assertEqual(notes, [])
        self.assertEqual(tracker.pack_detail(), "writing 3 car packages")
        notes = tracker.handle("packages_failed", error="boom")
        self.assertTrue(any("car packages failed" in text for _, text in notes))


if __name__ == "__main__":
    unittest.main()
