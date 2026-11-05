"""port_batch --packages (write_packages) and install.py's package install, without a real build."""

import argparse
import json
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
        self.cars = [{"media": "CAR_A_10"}, {"media": "CAR_B_10"}]
        self.events = []

    def progress(self, event, **data):
        self.events.append((event, data))

    def fake_pack(self, written: list[str]):
        def pack(build, media, out_dir, isos=None):
            self.assertEqual((build, media), (self.out, ["CAR_A_10", "CAR_B_10"]))
            self.assertEqual(out_dir, self.out / (port_batch.PACKAGES + ".new"))  # staged, swapped in whole
            self.assertIsInstance(isos, list)  # the build's disc images (gui_isos): names and logos
            out_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for name in written:
                (out_dir / name).write_bytes(b"new")
                paths.append(out_dir / name)
            return paths

        return pack

    def test_writes_a_whole_new_set(self):
        folder = self.out / port_batch.PACKAGES
        (folder / "forza.fh1.GONE_10.dsfcar").write_bytes(b"old")
        names = ["forza.fh1.CAR_A_10.dsfcar", "forza.fh1.CAR_B_10.dsfcar"]
        timings = {}
        with mock.patch.object(dsfcar, "pack", side_effect=self.fake_pack(names)):
            port_batch.write_packages(self.args, self.out, self.cars, timings, self.progress)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), names)
        self.assertFalse((self.out / (port_batch.PACKAGES + ".new")).exists())
        self.assertEqual(timings["packages"], 2)
        self.assertEqual(self.events[0], ("packages", {"cars": 2}))

    def test_a_failure_leaves_no_packages(self):
        """Reported, not raised (the build stands); no package of this or an earlier build is
        left for an install to take, and the report says why."""
        folder = self.out / port_batch.PACKAGES
        (folder / "forza.fh1.CAR_A_10.dsfcar").write_bytes(b"old")
        for side_effect in (ValueError("no VO3D"), self.fake_pack(["forza.fh1.CAR_A_10.dsfcar"])):
            timings = {}
            with mock.patch.object(dsfcar, "pack", side_effect=side_effect):
                port_batch.write_packages(self.args, self.out, self.cars, timings, self.progress)
            self.assertIn("packages_error", timings)
            self.assertFalse(folder.exists())
            self.assertFalse((self.out / (port_batch.PACKAGES + ".new")).exists())
            self.assertEqual(self.events[-1][0], "packages_failed")
        self.assertIn("1 packages for 2 cars", timings["packages_error"])


class PackageProblemTest(unittest.TestCase):
    def setUp(self):
        self.build = Path(tempfile.mkdtemp())
        cars = [{"media": "CAR_A_10", "game": "FH1"}, {"media": "CAR_B_10", "game": "FM4"}]
        (self.build / "dngvehicles.json").write_text(json.dumps({"cars": cars}))
        (self.build / "batch-report.json").write_text(json.dumps({"timings_s": {"packages": 2}}))
        (self.build / "packages").mkdir()

    def add(self, key: str) -> None:
        (self.build / "packages" / f"{key}.dsfcar").write_bytes(b"x")

    def test_problems(self):
        self.assertIn("no car packages", install.package_problem(self.build))
        self.add("forza.fh1.CAR_A_10")
        self.assertIn("1 cars without a package", install.package_problem(self.build))
        self.add("forza.fm4.CAR_B_10")
        self.assertIsNone(install.package_problem(self.build))
        self.add("forza.fh1.OTHER_10")
        self.assertIn("1 packages of other cars", install.package_problem(self.build))
        (self.build / "packages" / "forza.fh1.OTHER_10.dsfcar").unlink()
        (self.build / "batch-report.json").write_text(json.dumps({"timings_s": {"packages_error": "ValueError: x"}}))
        self.assertIn("failed (ValueError: x)", install.package_problem(self.build))


class InstallFlagsTest(unittest.TestCase):
    def test_install_ps1_gets_the_plugin_and_no_mode_switch(self):
        """Packages are the only install: install.ps1 has no -Packages, -Asi or -IdPolicy."""
        run = install.Run.__new__(install.Run)
        run.args = argparse.Namespace(skip_hook=False, no_write_profile=False, force=False)
        run.plugin = Path("out/asi/dsfcars.asi")
        run.record = None
        flags = run.install_flags(None, None)
        self.assertEqual(flags[:2], ["-AsiPath", str(run.plugin)])
        self.assertFalse({"-Packages", "-Asi", "-IdPolicy"} & set(flags))
        run.record = {"state": "installed"}  # an earlier install, merged or not: install.ps1 replaces it
        self.assertIn("-Force", run.install_flags(None, None))

    def test_tracker_shows_the_packages_step(self):
        tracker = install.Tracker()
        notes = tracker.handle("packages", cars=3)
        self.assertEqual(notes, [])
        self.assertEqual(tracker.pack_detail(), "writing 3 car packages")
        notes = tracker.handle("packages_failed", error="boom")
        self.assertTrue(any("car packages failed" in text for _, text in notes))


if __name__ == "__main__":
    unittest.main()
