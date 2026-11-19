"""The builder version, and the rule that decides when a car built earlier is reused
(port_batch build, dsff install; research/batch.md, "Reuse").

A built car is reused when all of these match the earlier build:

- the builder version (`current`);
- its media, with the same port_car options;
- its build ID (spool ID: the conversion writes it into more than the ID fields, e.g. its
  resource UIDs and Wwise IDs);
- its own source (`source_identity`): the disc it comes from by identity (the disc's
  name, e.g. "FM4 disc 1", from its default.xex) and its path on the disc, or its DLC pack
  by name; not the file name or path of the image or the pack.

Anything else converts. Adding discs or DLC packs, or renaming or moving them, converts
no car that doesn't come from them.

The builder version is one for every car:

- a release (the zip players get: src/dsfforza/build/package.py writes VERSION): its
  version, the tag (e.g. "v0.3.0");
- a git checkout: the commit. With uncommitted changes under DIRTY_PATHS (what the
  converter reads: its code, the data tables it reads, the locked dependencies) it is the
  commit with DIRTY appended, which never matches: no car is reused, kept or put in the car
  store, not even within one dirty tree;
- else (no VERSION, no git): None, no reuse.

`Memo` keeps small results computed under one builder version (the door counts of
port_batch's plan).
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import subprocess
from pathlib import Path

import dsfforza

DIRTY = "+dirty"
# What the converter reads from the checkout: an uncommitted change to any of these makes the
# version dirty. Not the docs, the tests or the car lists (they choose cars, not their bytes).
DIRTY_PATHS = (
    "src",
    "research/vehicles.tsv",
    "research/forza-roster.tsv",
    "tools/re",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)
VERSION_FILE = "VERSION"  # in a release: "<tag> <commit>" (package.build_zip)


def read(repo: Path | None = None) -> str | None:
    """The builder version of the checkout or release at `repo` (default this one), read now."""
    repo = Path(repo or dsfforza.REPO)
    if (repo / ".git").exists():
        return _git_version(repo)
    try:
        words = (repo / VERSION_FILE).read_text(encoding="utf-8").split()
    except OSError:
        return None
    return f"release {words[0]}" if words else None


def _git_version(repo: Path) -> str | None:
    head = _git(repo, "rev-parse", "HEAD")
    if not head:
        return None
    changed = _git(repo, "status", "--porcelain", "--untracked-files=normal", "--", *DIRTY_PATHS)
    return f"git {head}" + (DIRTY if changed else "")


def _git(repo: Path, *argv: str) -> str | None:
    try:
        out = subprocess.run(["git", *argv], cwd=repo, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


@functools.cache
def current() -> str | None:
    """This checkout's builder version, read once per process (`read`)."""
    return read()


def reusable(version: str | None) -> bool:
    """Whether cars built under `version` may be reused (by a build of the same version)."""
    return bool(version) and not version.endswith(DIRTY)


def source_identity(source: dict | None) -> dict:
    """A job's car source (forza_roster.Source as a dict) without the paths of its disc image
    and DLC pack file: its disc is in "where" ("FH1", "FM4 disc 2 / <pack on the disc>"), a
    DLC pack's name too ("FM4 DLC / <pack name>"). A pack on a disc keeps "package", its path
    on the disc."""
    out = dict(source or {})
    dlc = bool(out.get("package")) and not out.get("iso")
    out.pop("iso", None)
    if dlc:
        out.pop("package", None)
    return out


def car_key(job: dict) -> dict:
    """What a built car is reused by (the module docstring): its job's builder version,
    media, requested options (a fallback build's job keeps them), build ID and source."""
    return {
        "builder": job.get("builder"),
        "media": job.get("media"),
        "options": list(job.get("requested_options", job.get("port_options")) or []),
        "spool_id": job.get("spool_id"),
        "source": source_identity(job.get("source")),
    }


def digest(value) -> str:
    """A short digest of a JSON-able value (a car key)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:24]


KEY_NAMES = {
    "builder": "builder version",
    "media": "media",
    "options": "options",
    "spool_id": "build ID",
    "source": "source",
}


def why_not(old_job: dict | None, job: dict) -> str | None:
    """None when a car built from `old_job` is reused for `job`, else why not (the first
    difference)."""
    if not reusable(job.get("builder")):
        return f"builder version {job.get('builder') or 'unknown'} (no reuse)"
    if not old_job:
        return "no job record"
    old, new = car_key(old_job), car_key(job)
    for k, name in KEY_NAMES.items():
        if old[k] != new[k]:
            return name
    return None


# --- results kept per builder version --------------------------------------------------------

MEMO_VERSIONS = 6  # the builder versions a memo file keeps (several worktrees share out/cache)


def _memo_task(func: str, args: tuple):
    """In a Memo.fill process: module:function(*args)."""
    import importlib

    module, name = func.split(":")
    return getattr(importlib.import_module(module), name)(*args)


class Memo:
    """Small results kept between runs in one JSON file, under the builder version that
    computed them: a result is used by a later run of the same version. A key must name the
    inputs the result depends on (host_select.door_key: the car folder's files). Under a
    version that isn't reusable (a dirty tree) nothing is read or kept."""

    def __init__(self, path: Path, version: str | None = None):
        self.path = Path(path)
        self.version = current() if version is None else version
        self.entries: dict = self._load().get(self.version, {}) if reusable(self.version) else {}
        self.new: dict = {}

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        versions = data.get("versions") if isinstance(data, dict) else None
        return versions if isinstance(versions, dict) else {}

    def valid(self, key: str) -> bool:
        return key in self.new or key in self.entries

    def get(self, key: str, compute):
        """The kept result for `key`, else compute() (a JSON-able value), kept by save()."""
        if key in self.new:
            return self.new[key]
        if key in self.entries:
            return self.entries[key]
        value = compute()
        self.new[key] = value
        return value

    def fill(self, tasks: list[tuple[str, str, tuple]], jobs: int) -> None:
        """Compute the results get() would compute for `tasks` ((key, "module:function",
        args)) in `jobs` processes of the job pool, those not kept yet."""
        todo = [(key, func, args) for key, func, args in tasks if not self.valid(key)]
        if not todo:
            return
        from dsfforza.build import jobpool
        from dsfforza.build.port_batch import MEMO_RAM

        with jobpool.executor(max(1, min(jobs, len(todo))), "memo", MEMO_RAM) as pool:
            results = pool.map(_memo_task, [f for _, f, _ in todo], [a for _, _, a in todo], chunksize=4)
            for (key, _, _), value in zip(todo, results, strict=True):
                self.new[key] = value

    def save(self) -> None:
        """Keep the new results (with what another process kept meanwhile) under this version,
        and the MEMO_VERSIONS versions saved last."""
        if not self.new or not reusable(self.version) or read() != self.version:
            return  # nothing new, or the code changed while they were computed
        versions = self._load()
        entries = {**versions.pop(self.version, {}), **self.new}
        kept = dict(list(versions.items())[-(MEMO_VERSIONS - 1) :]) if MEMO_VERSIONS > 1 else {}
        kept[self.version] = dict(sorted(entries.items()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        temp.write_text(json.dumps({"versions": kept}, indent=0), encoding="utf-8")
        temp.replace(self.path)
        self.entries, self.new = kept[self.version], {}
