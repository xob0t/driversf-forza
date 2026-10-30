"""Per-car build fingerprints: what a port_batch worker read and which code it ran, so a
later build can reuse the car's output when none of it changed (port_batch build
--reuse, research/batch.md "Selective rebuild").

A worker records, while it converts one car:

- every file it opens for reading (an audit hook on "open" and "sqlite3.connect"),
  except Python's own files and this package's sources, with a digest: SHA-256 for
  files up to 64 MiB, else size, mtime and the SHA-256 of the first and last 4 MiB
  (the vanilla archive, the disc images);
- every directory it lists (os.listdir, os.scandir), as a digest of the names;
- the code it ran (sys.monitoring PY_START, each code object once), as units of
  the package's modules: a function or method body, a class body without its
  methods, a module-level assignment, and the rest of a module (imports and other
  top-level statements). A unit's hash is its AST without docstrings, so comments
  and formatting don't count. The car depends on the units it executed, the
  module-level names its executed code mentions (a constant used by a function the
  car ran), and the top level of every package module it had loaded.

`check` compares a stored fingerprint with the current files and sources; a car is
reusable only when every recorded input and unit is unchanged.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import sys
from pathlib import Path

import dsfforza

FORMAT = 1
BIG = 64 << 20  # files above this get a partial digest
EDGE = 4 << 20
PACKAGE = Path(dsfforza.__file__).resolve().parent
# module_units of every source text met, by its SHA-256 (CodeUnits): a worker would parse
# some 50 modules again for each car's fingerprint. Not an input of any fingerprint.
UNITS_CACHE = dsfforza.REPO / "out" / "cache" / "code-units"
MODULE_UNIT = "<module>"
TOOL_NAME = "dsfforza-fingerprint"  # the sys.monitoring tool a Recorder registers


# --- digests ------------------------------------------------------------------------


def norm(path) -> str:
    """A path as recorded: absolute, links and junctions resolved, case-folded on Windows."""
    return os.path.normcase(os.path.realpath(os.fspath(path)))


def file_digest(path: str) -> str:
    target = Path(path)
    try:
        size, mtime = target.stat().st_size, target.stat().st_mtime_ns
        with target.open("rb") as handle:
            if size <= BIG:
                return "sha256:" + hashlib.file_digest(handle, "sha256").hexdigest()
            h = hashlib.sha256(handle.read(EDGE))
            handle.seek(size - EDGE)
            h.update(handle.read(EDGE))
            return f"big:{size}:{mtime}:{h.hexdigest()}"
    except FileNotFoundError:
        return "missing"
    except IsADirectoryError:
        return "directory"
    except PermissionError:
        return "directory" if target.is_dir() else "unreadable"
    except OSError:
        return "unreadable"


def dir_digest(path: str) -> str:
    try:
        names = sorted(p.name for p in Path(path).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unreadable"
    return "names:" + hashlib.sha256("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()


# --- code units -----------------------------------------------------------------------


def _strip_docstring(body: list) -> list:
    first = body[0] if body else None
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
        return body[1:]
    return body


def _digest(node) -> str:
    return hashlib.sha256(ast.dump(node).encode()).hexdigest()[:24]


def _function_unit(node) -> str:
    copy = ast.FunctionDef if isinstance(node, ast.FunctionDef) else ast.AsyncFunctionDef
    return _digest(
        copy(
            name=node.name,
            args=node.args,
            body=_strip_docstring(node.body),
            decorator_list=node.decorator_list,
            returns=node.returns,
            type_params=getattr(node, "type_params", []),
        )
    )


def _class_units(node: ast.ClassDef, prefix: str, out: dict) -> None:
    name = prefix + node.name
    rest = []
    for stmt in _strip_docstring(node.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            out[f"{name}.{stmt.name}"] = _function_unit(stmt)
            rest.append(ast.Name(id=f"def {stmt.name}"))
        elif isinstance(stmt, ast.ClassDef):
            _class_units(stmt, name + ".", out)
            rest.append(ast.Name(id=f"class {stmt.name}"))
        else:
            rest.append(stmt)
    out[name] = _digest(
        ast.ClassDef(
            name=node.name,
            bases=node.bases,
            keywords=node.keywords,
            body=rest,
            decorator_list=node.decorator_list,
            type_params=getattr(node, "type_params", []),
        )
    )


def module_units(source: str) -> dict[str, str]:
    """{unit name: hash} of one module: "f" (function), "C" (class body without its
    methods), "C.m" (method), "=NAME" (a module-level assignment or import of NAME),
    "<module>" (the other top-level statements, defs inside them included)."""
    tree = ast.parse(source)
    out: dict[str, str] = {}
    rest = []
    for stmt in _strip_docstring(tree.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
            out[stmt.name] = _function_unit(stmt)
        elif isinstance(stmt, ast.ClassDef):
            _class_units(stmt, "", out)
        elif isinstance(stmt, ast.Import | ast.ImportFrom):
            # One unit per bound name, so adding an import doesn't count for the cars
            # whose code never mentions it.
            for alias in stmt.names:
                bound = alias.asname or alias.name.split(".")[0]
                out["=" + bound] = _digest(
                    ast.ImportFrom(module=getattr(stmt, "module", None), names=[alias], level=getattr(stmt, "level", 0))
                )
        elif isinstance(stmt, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            names = [n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)]
            if names:
                for name in names:
                    out["=" + name] = _digest(stmt)
            else:
                rest.append(stmt)
        else:
            rest.append(stmt)
    out[MODULE_UNIT] = _digest(ast.Module(body=rest, type_ignores=[]))
    return out


@functools.cache  # a car's code objects come from some 60 files; resolve() is slow on Windows
def module_name(path: str) -> str | None:
    """dsfforza.<group>.<module> for a source file of the package, else None."""
    try:
        rel = Path(path).resolve().relative_to(PACKAGE)
    except ValueError:
        return None
    if rel.suffix != ".py":
        return None
    parts = ["dsfforza", *rel.with_suffix("").parts]
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def unit_of(qualname: str) -> str:
    """The unit a code object belongs to: its enclosing top-level function, method or
    class (nested functions, lambdas and comprehensions are part of it); module-level
    lambdas and comprehensions are the module's top level."""
    name = qualname.split(".<locals>", 1)[0]
    return MODULE_UNIT if name.startswith("<") else name


class CodeUnits:
    """The current units of every package module, read from the sources once."""

    def __init__(self):
        self._units: dict[str, dict[str, str] | None] = {}

    def get(self, module: str) -> dict[str, str] | None:
        if module not in self._units:
            path = PACKAGE.joinpath(*module.split(".")[1:])
            source = path / "__init__.py" if path.is_dir() else path.with_suffix(".py")
            try:
                self._units[module] = cached_module_units(source.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                self._units[module] = None
        return self._units[module]


def cached_module_units(source: str) -> dict[str, str]:
    """module_units, kept in UNITS_CACHE by the source text's SHA-256 (and the Python
    version, whose ast.dump the digests come from)."""
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    path = UNITS_CACHE / f"{FORMAT}-py{sys.version_info[0]}{sys.version_info[1]}-{digest}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    units = module_units(source)
    temp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(json.dumps(units), encoding="utf-8")
        temp.replace(path)
    except OSError:  # another process wrote it meanwhile, or no room: the units are still right
        temp.unlink(missing_ok=True)
    return units


def package_hashes() -> dict[str, str]:
    """SHA-256 of every package source file (to notice an edit while a worker runs)."""
    return {
        str(p.relative_to(PACKAGE)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(PACKAGE.rglob("*.py"))
        if "__pycache__" not in p.parts
    }


def tree_digest(tree: dict[str, str]) -> str:
    """One digest of package_hashes()."""
    return hashlib.sha256(json.dumps(tree, sort_keys=True).encode()).hexdigest()


def combine(a: dict, b: dict) -> dict:
    """One fingerprint of two made of the same sources (both checked unchanged): the
    union of their inputs and code; complete when both are. The job is a's."""
    code = {m: dict(u) for m, u in a.get("code", {}).items()}
    for module, units in b.get("code", {}).items():
        code.setdefault(module, {}).update(units)
    return {
        **a,
        "complete": bool(a.get("complete")) and bool(b.get("complete")),
        "files": dict(sorted({**a.get("files", {}), **b.get("files", {})}.items())),
        "dirs": dict(sorted({**a.get("dirs", {}), **b.get("dirs", {})}.items())),
        "code": {m: dict(sorted(code[m].items())) for m in sorted(code)},
    }


# --- recording in a worker ----------------------------------------------------------------

_SKIP_PREFIXES = tuple(
    sorted(
        {norm(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, PACKAGE, UNITS_CACHE) if p},
        key=len,
        reverse=True,
    )
)


class Recorder:
    """Records a worker's reads and executed code from start() on."""

    def __init__(self, outputs: list[Path] | None = None):
        self.opened: list = []  # (path, mode, flags) as the audit hook saw them
        self.listed: list = []
        self.codes: set = set()
        self.outputs = tuple(norm(p) for p in (outputs or []))
        self.tool = None
        self.tree = None
        self.active = False

    def start(self) -> Recorder:
        self.tree = package_hashes()
        self.active = True
        sys.addaudithook(self._audit)
        mon = sys.monitoring
        mon.restart_events()  # code an earlier recorder in this process saw once (DISABLE)
        for tool in (4, 3, mon.COVERAGE_ID):
            if mon.get_tool(tool) is None:
                mon.use_tool_id(tool, TOOL_NAME)
                mon.register_callback(tool, mon.events.PY_START, self._started)
                mon.set_events(tool, mon.events.PY_START)
                self.tool = tool
                break
        return self

    def _audit(self, event: str, args) -> None:
        # Keep this cheap and free of audited calls: just remember the raw arguments.
        if not self.active:  # an audit hook can't be removed: after finish() it ignores all
            return
        if event == "open":
            self.opened.append(args)
        elif event in ("os.listdir", "os.scandir"):
            self.listed.append(args[0] if args else ".")
        elif event == "sqlite3.connect":
            self.opened.append((args[0], "sqlite", 0))

    def _started(self, code, offset, disable=sys.monitoring.DISABLE):
        # DISABLE bound at definition: a recorder still running at interpreter exit is called
        # after this module's globals (sys among them) are cleared.
        self.codes.add(code)
        return disable

    def stop(self) -> None:
        self.active = False
        if self.tool is not None:
            sys.monitoring.set_events(self.tool, 0)
            sys.monitoring.register_callback(self.tool, sys.monitoring.events.PY_START, None)
            sys.monitoring.free_tool_id(self.tool)
            self.tool = None

    def _inputs(self, opened=None, listed=None) -> tuple[set[str], set[str]]:
        files, dirs = set(), set()
        for path, mode, flags in self.opened if opened is None else opened:
            if isinstance(path, int) or path is None:
                continue  # an open file descriptor: its path was recorded when it was opened
            if mode == "sqlite":
                path = os.fspath(path)
                if isinstance(path, str) and path.startswith("file:"):  # a URI: file:<path>?mode=ro
                    path = path[5:].split("?", 1)[0]
                if path in ("", ":memory:"):
                    continue
                mode = "r"
            if mode is not None and any(c in str(mode) for c in "wax+"):
                continue  # written: an output (or a cache entry; it is recorded when read)
            if mode is None and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND):
                continue
            try:
                files.add(norm(path))
            except (TypeError, ValueError, OSError):
                continue
        for path in self.listed if listed is None else listed:
            if isinstance(path, int):
                continue
            try:
                dirs.add(norm(path))
            except (TypeError, ValueError, OSError):
                continue

        def keep(p: str) -> bool:
            return (
                not p.startswith(_SKIP_PREFIXES)
                and not p.startswith(self.outputs)
                and "__pycache__" not in p
                and not p.endswith(".tmp")
            )

        return {p for p in files if keep(p)}, {p for p in dirs if keep(p)}

    def _wanted(self, units: CodeUnits, codes=None) -> tuple[dict[str, set[str]], bool]:
        """({module: the units the recorded code depends on}, every module's source read)."""
        # A package module run as a script (python -m dsfforza.formats.upvh) is __main__.
        live_of = {}
        for key, m in list(sys.modules.items()):
            if (key.startswith("dsfforza") or key == "__main__") and getattr(m, "__file__", None):
                name = module_name(m.__file__)
                if name is not None and name != __name__:
                    live_of.setdefault(name, m)
        names: set[str] = set()
        executed: dict[str, set[str]] = {}
        for code in self.codes if codes is None else codes:
            module = module_name(code.co_filename)
            if module is None or module == __name__:  # the recorder itself shapes no output
                continue
            executed.setdefault(module, set()).add(unit_of(code.co_qualname))
            names.update(code.co_names)
        out: dict[str, set[str]] = {}
        complete = True
        for module in sorted(live_of):
            table = units.get(module)
            if table is None:
                complete = False
                continue
            wanted = {MODULE_UNIT} | executed.get(module, set())
            wanted |= {u for u in table if u.startswith("=") and u[1:] in names}
            # A class the car's code names: its body (fields, defaults) ran at import, maybe
            # before recording started. A function merely named counts only if it ran.
            live = live_of[module]
            wanted |= {
                u
                for u in table
                if not u.startswith("=") and "." not in u and u in names and isinstance(getattr(live, u, None), type)
            }
            # A def inside a top-level if/try block is part of the module's top level.
            out[module] = {unit for unit in wanted if unit in table}
        return out, complete

    def drain(self) -> dict:
        """What this process recorded since start() or the last drain(), JSON-able: a
        subprocess's share of a fingerprint its parent makes (finish(extra=...)). The
        recording goes on; each code object is reported once."""
        # What was recorded so far (this method's own reads and code go on being recorded).
        opened, listed, codes = list(self.opened), list(self.listed), set(self.codes)
        del self.opened[: len(opened)]
        del self.listed[: len(listed)]
        self.codes -= codes
        files, dirs = self._inputs(opened, listed)
        wanted, complete = self._wanted(CodeUnits(), codes)
        return {
            "tree": tree_digest(self.tree or {}),
            "complete": complete and package_hashes() == self.tree,
            "files": sorted(files),
            "dirs": sorted(dirs),
            "wanted": {m: sorted(u) for m, u in wanted.items()},
        }

    def finish(self, job: dict, extra: list[dict] = (), skip: tuple[str, ...] = ()) -> dict:
        """The fingerprint (a JSON-able dict) of everything recorded since start(), plus
        `extra`: drain() records of subprocesses that ran with the same sources. `skip`:
        path prefixes (norm) whose files and folders are not inputs."""
        self.stop()
        files, dirs = self._inputs()
        units = CodeUnits()
        wanted, complete = self._wanted(units)
        complete = complete and package_hashes() == self.tree
        tree = tree_digest(self.tree or {})
        for part in extra:
            complete = complete and part.get("complete", False) and part.get("tree") == tree
            files |= set(part.get("files", []))
            dirs |= set(part.get("dirs", []))
            for module, names in part.get("wanted", {}).items():
                wanted.setdefault(module, set()).update(names)
        if skip:
            files = {p for p in files if not p.startswith(skip)}
            dirs = {p for p in dirs if not p.startswith(skip)}
        code_fp: dict[str, dict[str, str]] = {}
        for module in sorted(wanted):
            table = units.get(module)
            if table is None:
                complete = False
                continue
            code_fp[module] = {unit: table[unit] for unit in sorted(wanted[module]) if unit in table}
        return {
            "format": FORMAT,
            "complete": complete,
            "job": job_key(job),
            "env": environment(),
            "files": {p: file_digest(p) for p in sorted(files)},
            "dirs": {p: dir_digest(p) for p in sorted(dirs)},
            "code": code_fp,
        }


# --- the job and environment ------------------------------------------------------------

JOB_FIELDS = ("media", "game", "year", "games", "source", "spool_id")
JOB_PATHS = ("dsf", "game_dir", "cache")


def job_key(job: dict) -> dict:
    """The parts of a port_batch job that decide the car's output (not where it goes)."""
    out = {k: job.get(k) for k in JOB_FIELDS}
    out["options"] = list(job.get("requested_options", job.get("port_options", [])))
    for k in JOB_PATHS:
        out[k] = norm(job[k]) if job.get(k) else None
    out["isos"] = [norm(p) for p in job.get("isos", [])]
    source = dict(out["source"] or {})
    if source.get("iso"):
        source["iso"] = norm(source["iso"])
    out["source"] = source
    return out


# The environment port_batch starts its workers with (run_workers). numpy's OpenBLAS
# splits work over its threads and adds the parts up in an order that can vary from run
# to run, so a car converted twice could come out a few hundred bytes different (vertex
# positions, a texture; BMW_Z4_11: three runs, three different chunks; one thread: the
# same chunk). One thread each: the workers are the parallelism. Not
# part of the fingerprint: a car built with BLAS threads is as good, just not repeatable.
WORKER_ENV = {"OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1", "MKL_NUM_THREADS": "1"}


def environment() -> dict:
    import numpy

    return {"python": sys.version, "numpy": numpy.__version__, "platform": sys.platform}


# --- checking a stored fingerprint --------------------------------------------------------


class Checker:
    """Compares stored fingerprints with the current files and sources (digests and
    module units computed once per build)."""

    def __init__(self):
        self.units = CodeUnits()
        self._files: dict[str, str] = {}
        self._dirs: dict[str, str] = {}
        self.env = environment()

    def file(self, path: str) -> str:
        if path not in self._files:
            self._files[path] = file_digest(path)
        return self._files[path]

    def dir(self, path: str) -> str:
        if path not in self._dirs:
            self._dirs[path] = dir_digest(path)
        return self._dirs[path]

    def moved(self, fp: dict, job: dict) -> dict[str, str]:
        r"""{the recorded vanilla archive path: the job's} when the job reads the vanilla archive
        from another place but it has the same digest. dsff install builds from the game's
        Install\dngvehicles.sp the first time and from the install's backup of it
        (dsfforza-backup\Install\dngvehicles.sp; tools/install.ps1 copies it with its
        time stamp) every time after, and the archive is the same file."""
        old, new = (fp.get("job") or {}).get("dsf"), job_key(job).get("dsf")
        recorded = fp.get("files", {}).get(old) if old else None
        if not new or old == new or recorded is None or self.file(new) != recorded:
            return {}
        return {old: new}

    def check(self, fp: dict, job: dict | None, code: bool = True, moved: dict | None = None) -> str | None:
        """None when nothing the fingerprint recorded changed, else the first reason.
        job None: don't compare the job (a first try, whose options were the job's
        before the fallback); code False: don't compare the code (--trust-code).
        moved: recorded input paths read from elsewhere now (default: the vanilla archive
        when it moved with the same digest, see `moved`)."""
        if fp.get("format") != FORMAT:
            return "no fingerprint of this format"
        if not fp.get("complete"):
            return "fingerprint incomplete (sources changed while the car was built)"
        if moved is None:
            moved = self.moved(fp, job) if job is not None else {}
        if job is not None:
            old, new = dict(fp.get("job") or {}), job_key(job)
            if old.get("dsf") in moved:
                old["dsf"] = moved[old["dsf"]]
            if old != new:
                return "job: " + ", ".join(k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k))
        if fp.get("env") != self.env:
            return "Python or numpy version"
        for path, digest in fp.get("files", {}).items():
            if self.file(moved.get(path, path)) != digest:
                return f"input {path}"
        for path, digest in fp.get("dirs", {}).items():
            if self.dir(path) != digest:
                return f"folder {path}"
        if code:
            for module, units in fp.get("code", {}).items():
                table = self.units.get(module)
                if table is None:
                    return f"code {module} (gone)"
                for unit, digest in units.items():
                    if table.get(unit) != digest:
                        return (
                            f"code {module}.{unit.lstrip('=')}" if unit != MODULE_UNIT else f"code {module} (top level)"
                        )
        return None


def read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- results kept between runs -------------------------------------------------------------


_MEMO_RECORDER: list = []  # a Memo.fill process's Recorder and CodeUnits


def _memo_task(func: str, args: tuple) -> tuple:
    """In a Memo.fill process: func(*args) recorded as Memo.get records it: (value, file
    stamps, folder digests, {module: units}, complete)."""
    import importlib

    if not _MEMO_RECORDER:
        _MEMO_RECORDER.extend((Recorder().start(), CodeUnits()))
    rec, units = _MEMO_RECORDER
    del rec.opened[:]
    del rec.listed[:]
    rec.codes.clear()
    sys.monitoring.restart_events()  # report code an earlier task of this process ran too
    module, name = func.split(":")
    value = getattr(importlib.import_module(module), name)(*args)
    opened, listed, codes = list(rec.opened), list(rec.listed), set(rec.codes)
    files, dirs = rec._inputs(opened, listed)
    wanted, complete = rec._wanted(units, codes)
    # (a package source changed meanwhile: Memo.save compares the package before and after)
    return (
        value,
        {p: stamp(p) for p in sorted(files)},
        {p: dir_digest(p) for p in sorted(dirs)},
        {m: sorted(u) for m, u in wanted.items()},
        complete,
    )


def stamp(path: str) -> str:
    """A file's size and modification time, or "missing"."""
    try:
        st = Path(path).stat()
    except OSError:
        return "missing"
    return f"{st.st_size}:{st.st_mtime_ns}"


class Memo:
    """Small results kept between runs in one JSON file, each with what computing it read
    and ran: the files' size and modification time, the folders' names and the code units
    (as a worker's fingerprint, but files by stamp, not digest). A result is used while
    all of that is unchanged. The code is that of every computation this process made
    for the memo, not just the result's own: one can skip code another ran (an import,
    a cache hit). Only where nothing else records (no Recorder of a worker or a pack runs
    in the process; then `get` just computes): port_batch's plan keeps the carbin door
    counts in one (host_select.DOORS_MEMO)."""

    def __init__(self, path: Path):
        self.path = path
        self.entries, self.codes = self._load()
        self.new: dict[str, dict] = {}
        self.units = CodeUnits()
        self.recorder: Recorder | None = None
        self.code: dict[str, set[str]] = {}  # every unit a computation of this process depended on
        self.tree: dict | None = None  # package_hashes() when fill() started
        self.checked: dict[str, bool] = {}  # code block -> unchanged

    def _load(self) -> tuple[dict, dict]:
        data = read(self.path)
        if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("env") != environment():
            return {}, {}
        return dict(data.get("entries", {})), dict(data.get("codes", {}))

    def _code_ok(self, block: str) -> bool:
        if block not in self.checked:
            units = self.codes.get(block)
            self.checked[block] = units is not None and all(
                (table := self.units.get(module)) is not None and all(table.get(u) == d for u, d in names.items())
                for module, names in units.items()
            )
        return self.checked[block]

    def _valid(self, entry: dict) -> bool:
        """Its inputs unchanged, and its code (an entry computed in this process, not saved
        yet, has no code block: it ran the current code)."""
        return (
            all(stamp(p) == s for p, s in entry.get("files", {}).items())
            and all(dir_digest(p) == d for p, d in entry.get("dirs", {}).items())
            and ("code" not in entry or self._code_ok(entry["code"]))
        )

    def get(self, key: str, compute):
        """The kept result for `key` when nothing it depended on changed, else compute()
        (a JSON-able value), recorded and kept by save()."""
        entry = self.new.get(key) or self.entries.get(key)
        if entry is not None and self._valid(entry):
            return entry["value"]
        if self.recorder is None:
            mon = sys.monitoring
            if any(mon.get_tool(t) == TOOL_NAME for t in range(6)):  # a worker or pack records here
                return compute()
            self.recorder = Recorder().start()
        rec = self.recorder
        del rec.opened[:]
        del rec.listed[:]
        rec.codes.clear()
        sys.monitoring.restart_events()  # report code an earlier computation ran too
        value = compute()
        opened, listed, codes = list(rec.opened), list(rec.listed), set(rec.codes)
        files, dirs = rec._inputs(opened, listed)
        wanted, complete = rec._wanted(self.units, codes)
        for module, names in wanted.items():
            self.code.setdefault(module, set()).update(names)
        if complete:
            self.new[key] = {
                "value": value,
                "files": {p: stamp(p) for p in sorted(files)},
                "dirs": {p: dir_digest(p) for p in sorted(dirs)},
            }
        return value

    def valid(self, key: str) -> bool:
        """Whether a kept result for `key` can be used (get returns it without computing)."""
        entry = self.new.get(key) or self.entries.get(key)
        return entry is not None and self._valid(entry)

    def fill(self, tasks: list[tuple[str, str, tuple]], jobs: int) -> None:
        """Compute the results get() would compute for `tasks` ((key, "module:function",
        args): get(key, lambda: function(*args))) in `jobs` processes, each recording what
        its computations read and ran as get does; the ones still valid are skipped. Only
        where get records (see the class)."""
        todo = [(key, func, args) for key, func, args in tasks if not self.valid(key)]
        if not todo or self.recorder is not None or any(sys.monitoring.get_tool(t) == TOOL_NAME for t in range(6)):
            return
        from concurrent.futures import ProcessPoolExecutor

        self.tree = self.tree or package_hashes()
        with ProcessPoolExecutor(max_workers=max(1, min(jobs, len(todo)))) as pool:
            results = pool.map(_memo_task, [f for _, f, _ in todo], [a for _, _, a in todo], chunksize=4)
            for (key, _, _), (value, files, dirs, wanted, complete) in zip(todo, results, strict=True):
                for module, names in wanted.items():
                    self.code.setdefault(module, set()).update(names)
                if complete:
                    self.new[key] = {"value": value, "files": files, "dirs": dirs}

    def save(self) -> None:
        """Stop recording and keep the new results (with the entries another process kept
        meanwhile), unless a package source changed while they were computed."""
        rec, self.recorder = self.recorder, None
        if rec is None and self.tree is None:
            return
        if rec is not None:
            rec.stop()
        tree = self.tree if self.tree is not None else rec.tree
        if not self.new or package_hashes() != tree or (rec is not None and rec.tree != tree):
            return
        code = {}
        for module in sorted(self.code):
            table = self.units.get(module)
            if table is None:
                return
            code[module] = {u: table[u] for u in sorted(self.code[module]) if u in table}
        block = hashlib.sha256(json.dumps(code, sort_keys=True).encode()).hexdigest()[:24]
        entries, codes = self._load()
        codes[block] = code
        entries.update({k: {**v, "code": block} for k, v in self.new.items()})
        used = {e.get("code") for e in entries.values()}
        codes = {b: c for b, c in codes.items() if b in used}
        from dsfforza.build.port_car import write_atomic

        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"format": FORMAT, "env": environment(), "codes": codes, "entries": dict(sorted(entries.items()))}
        write_atomic(self.path, json.dumps(data, indent=0).encode())
        self.entries, self.codes, self.new = entries, codes, {}
        self.checked.clear()
