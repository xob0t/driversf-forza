"""Run every Python test file of the repo (tests/test_*.py) after `ruff check`,
`ruff format --check` and the stricter rules' ratchet (tests/ruff_ratchet.py). From the repo
root:

    uv run python -m tests                 # lint, format check, all tests
    uv run python -m tests formats asi     # only files whose name contains a word
    uv run python -m tests -v              # one line per test
    uv run python -m tests --no-lint       # tests only
    uv run python -m tests --ci            # without the files tests/ci-skip.txt lists (CI has no game)

Collects unittest.TestCase classes and plain module-level test_* functions
(assert-based; pytest collects the same files too). A test file whose import
needs a package that isn't installed (lupa for the register.lua test, in the
`test` dependency group) is reported as skipped, not passed. The PowerShell
tests (tools/wsb/test-host-session.ps1, tools/asi/test/test_install_asi.ps1)
and the plugin's offline test (tools/asi/build.ps1 -Test) are run on their own.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from tests import ruff_ratchet

REPO = Path(__file__).resolve().parents[1]
TEST_DIR = REPO / "tests"
CI_SKIP = TEST_DIR / "ci-skip.txt"  # test files that need the game, the Forza discs or out/


def ci_skipped() -> dict[str, str]:
    """{file name: why} from tests/ci-skip.txt (`test_x.py  # reason` per line)."""
    out = {}
    for line in CI_SKIP.read_text(encoding="utf-8").splitlines() if CI_SKIP.exists() else []:
        name, _, why = line.partition("#")
        if name.strip():
            out[name.strip()] = why.strip()
    return out


def test_files(words: list[str], ci: bool = False) -> list[Path]:
    files = sorted(TEST_DIR.glob("test_*.py"))
    if ci:
        skipped = ci_skipped()
        for name, why in skipped.items():
            print(f"skipped on CI: {name} ({why})")
        files = [p for p in files if p.name not in skipped]
    return [p for p in files if not words or any(w in p.stem for w in words)]


def load(path: Path) -> unittest.TestSuite:
    name = "repo_test_" + path.stem
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except ModuleNotFoundError as error:
        if (error.name or "").split(".")[0] == "dsfforza":
            raise  # a repo module: a real failure
        reason = f"{path.name}: needs {error.name} (uv sync --group test)"

        class Skipped(unittest.TestCase):
            def test_file(self):
                self.skipTest(reason)

        Skipped.__qualname__ = Skipped.__name__ = path.stem
        return unittest.defaultTestLoader.loadTestsFromTestCase(Skipped)
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    for attr, value in vars(module).items():
        if (
            attr.startswith("test_")
            and callable(value)
            and not isinstance(value, type)
            and getattr(value, "__module__", None) == name
        ):
            case = unittest.FunctionTestCase(value, description=f"{path.stem}.{attr}")
            suite.addTest(case)
    return suite


def lint() -> bool:
    """ruff check + ruff format --check over the repo (config in pyproject.toml), then the
    stricter rules' ratchet (tests/ruff_ratchet.py)."""
    if importlib.util.find_spec("ruff") is None:
        print("ruff not found (uv sync installs the dev group); lint skipped")
        return True
    ok = True
    for args in (["check"], ["format", "--check"]):
        print(f"ruff {' '.join(args)}", flush=True)
        ok &= subprocess.run([sys.executable, "-m", "ruff", *args], cwd=REPO, check=False).returncode == 0
    print("ruff ratchet", flush=True)
    return ruff_ratchet.check() and ok


def main(argv: list[str]) -> int:
    verbose = "-v" in argv
    words = [a for a in argv if not a.startswith("-")]
    linted = lint() if "--no-lint" not in argv else True
    files = test_files(words, ci="--ci" in argv)
    if not files:
        print(f"no test files match {words}")
        return 1
    suite = unittest.TestSuite()
    for path in files:
        print(f"{path.relative_to(REPO)}")
        suite.addTest(load(path))
    result = unittest.TextTestRunner(verbosity=2 if verbose else 1).run(suite)
    if not linted:
        print("ruff found problems (see above)")
    return 0 if result.wasSuccessful() and linted else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
