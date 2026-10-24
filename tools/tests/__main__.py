"""Run every Python test file of the repo: tools/tests/test_*.py and
tools/asi/test/test_*.py. From the repo root:

    python -m tools.tests                 # all of them
    python -m tools.tests formats asi     # only files whose name contains a word
    python -m tools.tests -v              # one line per test

Collects unittest.TestCase classes and plain module-level test_* functions
(assert-based; pytest collects the same files too). Standard library only. A
test file whose import needs a package that isn't installed (lupa for the
register.lua test) is reported as skipped, not passed. The PowerShell tests
(tools/wsb/test-host-session.ps1, tools/asi/test/test_install_asi.ps1) and the
plugin's offline test (tools/asi/build.ps1 -Test) are run on their own.
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TEST_DIRS = (REPO / "tools" / "tests", REPO / "tools" / "asi" / "test")


def test_files(words: list[str]) -> list[Path]:
    files = sorted(p for d in TEST_DIRS for p in d.glob("test_*.py"))
    return [p for p in files if not words or any(w in p.stem for w in words)]


def load(path: Path) -> unittest.TestSuite:
    name = "repo_test_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as error:
        if (REPO / "tools" / f"{error.name}.py").exists():
            raise   # a repo module: a real failure
        reason = f"{path.name}: needs {error.name} (pip install {error.name})"

        class Skipped(unittest.TestCase):
            def test_file(self):
                self.skipTest(reason)
        Skipped.__qualname__ = Skipped.__name__ = path.stem
        return unittest.defaultTestLoader.loadTestsFromTestCase(Skipped)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    for attr, value in vars(module).items():
        if attr.startswith("test_") and callable(value) and not isinstance(value, type) \
                and getattr(value, "__module__", None) == name:
            case = unittest.FunctionTestCase(value, description=f"{path.stem}.{attr}")
            suite.addTest(case)
    return suite


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    words = [a for a in argv if not a.startswith("-")]
    files = test_files(words)
    if not files:
        print(f"no test files match {words}")
        return 1
    suite = unittest.TestSuite()
    for path in files:
        print(f"{path.relative_to(REPO)}")
        suite.addTest(load(path))
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
