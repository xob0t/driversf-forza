"""The stricter ruff rules, with a ratchet: code that broke them before they were enabled is
counted per file and rule in tests/ruff-ratchet.json, and a file may not go above its count.
New files start at zero. When a file's count drops, the baseline is lowered to match, so the
list only shrinks. `uv run python -m tests` runs the check after `ruff check`.

    uv run python -m tests.ruff_ratchet            # check (exit 1 on new violations)
    uv run python -m tests.ruff_ratchet --show F   # every ratchet violation in files matching F
    uv run python -m tests.ruff_ratchet --update   # lower the baseline to the current counts

--update never raises a count: fix the new violation instead (or, for a deliberate
exception, a `# noqa: <code>` with the reason on that line). The rule limits (complexity,
branches, statements) are in pyproject.toml under [tool.ruff.lint.mccabe] and
[tool.ruff.lint.pylint].
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BASELINE = REPO / "tests" / "ruff-ratchet.json"

SELECT = [
    "BLE001",  # blind except
    "ARG",  # unused arguments
    "ERA001",  # commented-out code
    "TRY",  # exception handling
    "FBT001",  # boolean positional parameter
    "FBT002",  # boolean positional default
    "C901",  # cyclomatic complexity
    "PLR0912",  # too many branches
    "PLR0915",  # too many statements
]
IGNORE = [
    "TRY003",  # long messages in raise: every SystemExit/ValueError here names the file and value
]
# Reverse-engineering scripts, rewritten with each RE round: plain `ruff check` only.
EXCLUDE = ["tools/re"]


def violations() -> list[dict]:
    """ruff's findings for the ratchet rules, with posix paths relative to the repo."""
    command = [sys.executable, "-m", "ruff", "check", "--output-format", "json", "--exit-zero"]
    command += ["--select", ",".join(SELECT), "--ignore", ",".join(IGNORE), "--extend-exclude", ",".join(EXCLUDE), "."]
    result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, encoding="utf-8", check=True)
    found = json.loads(result.stdout)
    for item in found:
        item["path"] = Path(item["filename"]).resolve().relative_to(REPO).as_posix()
    return found


def counts(found: list[dict]) -> dict[str, dict[str, int]]:
    by_file: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for item in found:
        by_file[item["path"]][item["code"]] += 1
    return {path: dict(sorted(codes.items())) for path, codes in sorted(by_file.items())}


def load_baseline() -> dict[str, dict[str, int]]:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def write_baseline(baseline: dict[str, dict[str, int]]) -> None:
    # One file per line, sorted: parallel branches touching different files merge cleanly.
    lines = [f"  {json.dumps(path)}: {json.dumps(codes)}" for path, codes in sorted(baseline.items()) if codes]
    BASELINE.write_text("{\n" + ",\n".join(lines) + "\n}\n", encoding="utf-8", newline="\n")


def compare(current: dict, baseline: dict) -> tuple[list[tuple[str, str, int, int]], list[tuple[str, str, int, int]]]:
    """(over, under): (path, code, now, allowed) where a count went above or below the baseline."""
    over, under = [], []
    for path in sorted(current.keys() | baseline.keys()):
        now, allowed = current.get(path, {}), baseline.get(path, {})
        for code in sorted(now.keys() | allowed.keys()):
            n, a = now.get(code, 0), allowed.get(code, 0)
            if n > a:
                over.append((path, code, n, a))
            elif n < a:
                under.append((path, code, n, a))
    return over, under


def check() -> bool:
    found = violations()
    over, under = compare(counts(found), load_baseline())
    for path, code, n, a in over:
        print(f"ruff ratchet: {path}: {code} {n} > {a} allowed (fix the new one; the file's {code} findings:)")
        for item in found:
            if item["path"] == path and item["code"] == code:
                print(f"    {path}:{item['location']['row']}: {item['message']}")
    for path, code, n, a in under:
        print(f"ruff ratchet: {path}: {code} down to {n} from {a}; run `uv run python -m tests.ruff_ratchet --update`")
    if not over and not under:
        total = sum(sum(codes.values()) for codes in load_baseline().values())
        print(f"ruff ratchet: no new violations ({total} grandfathered)")
    return not over and not under


def update() -> int:
    current, baseline = counts(violations()), load_baseline()
    over, _ = compare(current, baseline)
    if over and BASELINE.exists():
        for path, code, n, a in over:
            print(f"ruff ratchet: {path}: {code} {n} > {a}; --update only lowers counts")
        return 1
    write_baseline(current)
    print(f"wrote {BASELINE.relative_to(REPO)}: {sum(sum(c.values()) for c in current.values())} grandfathered")
    return 0


def show(words: list[str]) -> int:
    for item in violations():
        if not words or any(w in item["path"] for w in words):
            print(f"{item['path']}:{item['location']['row']}: {item['code']} {item['message']}")
    return 0


def main(argv: list[str]) -> int:
    if "--update" in argv:
        return update()
    if "--show" in argv:
        return show([a for a in argv if not a.startswith("-")])
    return 0 if check() else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
