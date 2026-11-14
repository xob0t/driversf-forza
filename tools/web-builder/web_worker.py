"""Runs inside Pyodide (tools/web-builder/run-car.mjs): one port_batch worker job.

The converter is used as it is; only what has no meaning in a browser is shimmed here:

- port_batch.lower_priority (os.nice / SetPriorityClass): a Web Worker has no priority.

Writes /work/web-stats.json: the worker's exit code, its wall time and the modules it
imported. Bytes read per file are counted on the JS side (run-car.mjs), where every read
of a mounted file passes.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/repo/src")


def shim_numpy_32bit() -> None:
    """wasm32 numpy: np.intp is int32, and int64 count and index arrays no longer cast to it
    'safely' (TypeError). Each place found by running a car is shimmed here, casting such
    an argument to intp (its values are far below 2**31, so results are the same); the
    list is in research/web-builder.md, "32-bit numpy":
    - np.repeat(a, repeats): trim_art.triangles_cells (`size` is np.int64)
    - np.add.reduceat(a, indices): lights._texels (`at` is np.int64)
    - np.minimum.reduceat(a, indices): damage.uv.lookup_many (`first`)"""
    import numpy as np

    if np.dtype(np.intp).itemsize == 8:
        return
    repeat = np.repeat

    def repeat_any_int(a, repeats, axis=None):
        r = np.asarray(repeats)
        if r.dtype.kind in "iu" and r.dtype != np.intp:
            if r.size and int(r.max()) >= 2**31:
                raise OverflowError("repeat count past int32")
            repeats = r.astype(np.intp)
        return repeat(a, repeats, axis=axis)

    np.repeat = repeat_any_int

    class IntIndices:
        """A ufunc whose reduceat casts int64 `indices` to intp."""

        def __init__(self, ufunc):
            self._ufunc = ufunc

        def __call__(self, *args, **kwargs):
            return self._ufunc(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._ufunc, name)

        def reduceat(self, array, indices, *args, **kwargs):
            i = np.asarray(indices)
            if i.dtype.kind in "iu" and i.dtype != np.intp:
                indices = i.astype(np.intp)
            return self._ufunc.reduceat(array, indices, *args, **kwargs)

    for name in ("add", "minimum", "maximum"):
        setattr(np, name, IntIndices(getattr(np, name)))


def main(job: str) -> int:
    started = time.time()
    from dsfforza.build import port_batch

    shim_numpy_32bit()
    port_batch.lower_priority = lambda: None
    with Path("/work/worker.log").open("w", encoding="utf-8") as out:  # what the native parent captures
        sys.stdout = sys.stderr = out
        try:
            code = port_batch.worker(Path(job))
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    stats = {
        "code": code,
        "seconds": round(time.time() - started, 1),
        "python": sys.version,
        "numpy": __import__("numpy").__version__,
        "modules": sorted(m for m in sys.modules if not m.startswith("dsfforza") and "." not in m),
    }
    Path("/work/web-stats.json").write_text(json.dumps(stats, indent=1), encoding="utf-8")
    return code


def gui(build: str, out: str, isos: list[str]) -> int:
    """port_batch's --gui step for a finished build folder, in this process (jobs=1: no
    render processes): gui.sp, Fonts.fchunk, hud.lua and the garage pictures into `out`."""
    import os

    from dsfforza.game import gui_assets

    shim_numpy_32bit()
    os.environ["DSF_GAME_DIR"] = "/game"
    started = time.time()
    with Path(out, "gui.log").open("w", encoding="utf-8") as log:
        sys.stdout = sys.stderr = log
        try:
            gui_assets.build(
                Path(build, "dngvehicles.json"),
                Path(out),
                log=print,
                archive=Path(build, "dngvehicles.sp"),
                jobs=1,
                isos=[Path(i) for i in isos],
            )
        finally:
            sys.stdout, sys.stderr = sys.__stdout__, sys.__stderr__
    Path(out, "web-stats.json").write_text(json.dumps({"seconds": round(time.time() - started, 1)}), encoding="utf-8")
    return 0
