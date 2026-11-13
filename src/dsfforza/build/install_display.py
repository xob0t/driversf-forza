"""dsff install's display (src/dsfforza/build/install.py): port_batch's progress events
folded into the stages' states (Tracker), drawn live with rich on a terminal (LiveReporter) or
as plain lines (PlainReporter), and the summary at the end.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dsfforza import REPO

MIB = 1 << 20
GIB = 1 << 30

STAGES = (
    ("preflight", "Preflight"),
    ("prepare", "Prepare"),
    ("plugin", "Plugin"),
    ("build", "Build cars"),
    ("pack", "Pack"),
    ("install", "Install"),
)


def rel(path: Path) -> str:
    """A path relative to the repo when it is inside it, else as given."""
    try:
        return str(Path(path).resolve().relative_to(REPO))
    except ValueError:
        return str(path)


# --- the build's progress -----------------------------------------------------------------------


@dataclass
class CarFailure:
    """A car the build failed or left out, as the display and the summary show it."""

    media: str
    cause: str
    error: str = ""
    log: str | None = None


@dataclass
class Tracker:
    """port_batch's events (port_batch.emit) folded into what the display shows."""

    clock: object = time.monotonic
    phase: str = "plan"
    total: int = 0
    reused: int = 0
    todo: int = 0
    converted: int = 0
    jobs: int = 1
    gentle: bool = False
    seconds_hint: float | None = None  # a car's conversion time in the last build
    convert_started: float | None = None
    running: dict = field(default_factory=dict)  # media -> {"started", "note", "verify"}
    failures: list = field(default_factory=list)
    rebuilt: dict = field(default_factory=dict)  # why -> count
    verify: set = field(default_factory=set)
    verify_done: int = 0
    merge_cars: int = 0
    lint_cars: int = 0
    lint_total: int = 0
    pack_source: str = ""
    lint_failed: int = 0
    gui_total: int = 0
    packages_total: int = 0
    gui_done: int = 0
    gui_last: str = ""

    @property
    def stage(self) -> str:
        """The install stage the build's phase is in: build (plan, convert, verify), else
        pack."""
        return "build" if self.phase in ("plan", "convert", "verify") else "pack"

    @property
    def done(self) -> int:
        """The cars ready so far: reused and converted."""
        return self.reused + self.converted

    def rate(self) -> float | None:
        """Converted cars per minute so far."""
        if not self.converted or self.convert_started is None:
            return None
        return self.converted / max(1e-6, self.clock() - self.convert_started) * 60

    def eta(self) -> float | None:
        """Seconds until every car is converted: the rate so far, else the last build's
        time per car over the workers."""
        left = self.todo - self.converted
        if left <= 0:
            return 0.0
        rate = self.rate()
        if rate:
            return left / rate * 60
        if self.seconds_hint:
            return left * self.seconds_hint / max(1, self.jobs)
        return None

    def handle(self, event: str, **data) -> list[tuple[str, str]]:
        """Update from one event; returns (level, text) lines to show above the display.
        Event `x` is handled by `_on_x(now, data)`; an event without one changes nothing."""
        now = self.clock()
        handler = getattr(self, f"_on_{event}", None)
        return handler(now, data) if handler is not None else []

    def _on_plan(self, _now, data) -> list[tuple[str, str]]:
        self.total = len(data.get("cars", []))
        notes = []
        for s in data.get("skipped", []):
            failure = CarFailure(s["media"], "not selected", s.get("error", ""))
            self.failures.append(failure)
            notes.append(("error", f"{failure.media}: {failure.error or failure.cause}"))
        return notes

    def _on_convert(self, now, data) -> list[tuple[str, str]]:
        self.phase = "convert"
        self.todo = len(data.get("todo", []))
        self.reused = len(data.get("reused", []))
        self.jobs = data.get("jobs", self.jobs)
        self.gentle = data.get("gentle", False)
        self.convert_started = now
        for why in (data.get("rebuilt") or {}).values():
            key = reason_kind(why)
            self.rebuilt[key] = self.rebuilt.get(key, 0) + 1
        return []

    def _on_car_start(self, now, data) -> list[tuple[str, str]]:
        note = "verify" if data.get("verify") else (data.get("fallback") or "")
        self.running[data["media"]] = {"started": now, "note": note, "verify": bool(data.get("verify"))}
        return []

    def _on_car_retry(self, _now, data) -> list[tuple[str, str]]:
        return [("warn", f"{data['media']}: {data.get('cause') or 'failed'}; building again {data['why']}")]

    def _on_car_done(self, _now, data) -> list[tuple[str, str]]:
        media, report = data["media"], data.get("report") or {}
        info = self.running.pop(media, {})
        if info.get("verify") or media in self.verify:
            self.verify_done += 1
            return [] if report.get("ok") else [("error", f"{media} (reuse check): {report.get('cause')}")]
        self.converted += 1
        if report.get("ok"):
            return []
        failure = CarFailure(
            media, report.get("cause") or "error", str(report.get("error") or "")[:300], data.get("log")
        )
        self.failures.append(failure)
        return [("error", f"{media}: {failure.cause}" + (f" (log {failure.log})" if failure.log else ""))]

    def _on_verify(self, _now, data) -> list[tuple[str, str]]:
        self.phase = "verify"
        self.verify = set(data.get("cars", []))
        return []

    def _on_verify_mismatch(self, _now, data) -> list[tuple[str, str]]:  # port_batch --verify-mismatch rebuild
        bad, redo = data.get("cars", []), data.get("redo", [])
        self.reused = max(0, self.reused - len(redo))
        self.todo += len(redo)
        self.phase = "convert"
        return [
            (
                "warn",
                f"{', '.join(bad)}: came out different when converted again; using the new copy and "
                f"converting the {len(redo)} other reused cars again",
            )
        ]

    def _on_merge(self, _now, data) -> list[tuple[str, str]]:
        self.phase = "merge"
        self.merge_cars = data.get("cars", 0)
        return []

    def _on_car_failed(self, _now, data) -> list[tuple[str, str]]:
        failure = CarFailure(data["media"], data.get("cause") or "merge", str(data.get("error") or "")[:300])
        self.failures.append(failure)
        return [("error", f"{failure.media}: {failure.cause} ({failure.error[:120]})")]

    def _on_pack_reused(self, _now, data) -> list[tuple[str, str]]:
        self.phase = "pack_reused"
        self.pack_source = data.get("source", "")
        return []

    def _on_lint(self, _now, data) -> list[tuple[str, str]]:
        self.phase = "lint"
        self.lint_cars = data.get("cars", 0)
        self.lint_total = data.get("total", self.lint_cars)
        return []

    def _on_lint_failed(self, _now, data) -> list[tuple[str, str]]:
        self.lint_failed += 1
        return [("warn", f"{data['media']}: lint found problems (kept in the archive; see the log)")]

    def _on_gui(self, _now, data) -> list[tuple[str, str]]:
        self.phase = "gui"
        self.gui_total = data.get("cars", 0)
        return []

    def _on_gui_log(self, _now, data) -> list[tuple[str, str]]:
        self.gui_done = min(self.gui_total, self.gui_done + 1) if self.gui_total else self.gui_done + 1
        self.gui_last = " ".join(str(data.get("text", "")).split()[:2]).rstrip(":")
        return []

    def _on_gui_failed(self, _now, data) -> list[tuple[str, str]]:
        return [("warn", f"garage pictures and badges failed: {data.get('error')} (the cars still work)")]

    def _on_packages(self, _now, data) -> list[tuple[str, str]]:
        self.phase = "packages"
        self.packages_total = data.get("cars", 0)
        return []

    def _on_packages_failed(self, _now, data) -> list[tuple[str, str]]:
        return [("warn", f"car packages failed: {data.get('error')} (the build itself is usable)")]

    def _on_done(self, _now, _data) -> list[tuple[str, str]]:
        self.phase = "done"
        return []

    def pack_detail(self) -> str:
        """The pack stage's detail line for the current phase ("" outside the pack)."""
        if self.phase == "packages":
            return f"writing {self.packages_total} car packages"
        if self.phase == "merge":
            return f"merging {self.merge_cars} cars into one archive"
        if self.phase == "pack_reused":
            return f"unchanged since {rel(Path(self.pack_source))}: reusing its archive and garage pictures"
        if self.phase == "lint":
            if self.lint_cars < self.lint_total:
                return f"checking {self.lint_cars} of {self.lint_total} cars (upvh lint; the rest unchanged)"
            return "checking every car (upvh lint)"
        if self.phase == "gui":
            return f"garage pictures and badges {self.gui_done}/{self.gui_total}" + (
                f"  {self.gui_last}" if self.gui_last else ""
            )
        return ""


def pack_text(stats: dict | None, cars: int) -> str:
    """The pack step's detail: what it took from the last build (port_batch's
    timings_s.pack_reuse_stats, src/dsfforza/build/pack_reuse.py)."""
    if not stats:
        return "garage pictures, badges"
    if stats.get("whole"):
        return "unchanged: the last build's pack"
    linted = stats.get("linted")
    made = stats.get("pictures_made", 0)
    if linted is None or (linted >= cars and made >= cars):
        return "garage pictures, badges"
    return f"{linted} cars checked, {made} garage pictures drawn (the rest unchanged)"


def reason_kind(why: str) -> str:
    """port_batch.Reuse's reason a car is converted again, in a few words."""
    for prefix, kind in (
        ("code ", "code changed"),
        ("input ", "input changed"),
        ("folder ", "input changed"),
        ("job: ", "options changed"),
        ("first try: ", "code or input changed"),
        ("Python", "Python or numpy changed"),
    ):
        if why.startswith(prefix):
            return kind
    return why


def clock_text(seconds: float | None) -> str:
    """Seconds as m:ss or h:mm:ss, "--:--" when unknown."""
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def size_text(n: int | None) -> str:
    """Bytes as GiB (from 1 GiB on) or MiB, "?" when unknown."""
    if n is None:
        return "?"
    return f"{n / GIB:.2f} GiB" if n >= GIB else f"{n / MIB:.0f} MiB"


def archive_text(s) -> str:
    """The build's merged archive: its path and size, or that it wasn't written (past a
    CHNK's 4 GiB, or --no-archive: port_batch.register_cars; the packages don't need it)."""
    archive = s.build_dir / "dngvehicles.sp"
    if s.archive_bytes is None and not archive.is_file():
        return "not written (the packages don't need it; port_batch writes it below 4 GiB)"
    return f"{archive} ({size_text(s.archive_bytes)})"


# --- the display ------------------------------------------------------------------------------------


@dataclass
class Summary:
    """What the closing summary of a run shows: the build's cars and archive, the install,
    notes, the log."""

    elapsed: float = 0.0
    build_dir: Path | None = None
    in_archive: int = 0
    built: int = 0
    reused: int = 0
    failed: list = field(default_factory=list)  # CarFailure
    archive_bytes: int | None = None
    pack: str = ""  # what the pack step redid ("" before it ran)
    game: Path | None = None
    replaced: list = field(default_factory=list)
    created: list = field(default_factory=list)
    save_backup: Path | None = None
    log: Path | None = None
    notes: list = field(default_factory=list)
    installed: bool = False
    uninstall: str = "uv run dsff install --uninstall"


class Reporter:
    """Stage states and the build's events; LiveReporter and PlainReporter draw them."""

    def __init__(self):
        self.states = {key: ("pending", "") for key, _ in STAGES}
        self.started = {}
        self.tracker = Tracker()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stage(self, key: str, state: str, detail: str = "") -> None:
        """Set a stage's state and detail ("running" starts its clock) and show the change."""
        if state == "running" and self.states[key][0] != "running":
            self.started[key] = time.monotonic()
        self.states[key] = (state, detail)
        self.show_stage(key)

    def show_stage(self, key: str) -> None:
        """Show a stage's new state (each display draws it its own way)."""

    def note(self, text: str, level: str = "info") -> None:
        """Show a line above the display; level: info, warn or error."""

    def event(self, event: str, **data) -> None:
        """A port_batch progress event (port_batch.emit): fold it into the tracker, move the
        build and pack stages on, and show it."""
        before = self.tracker.stage
        for level, text in self.tracker.handle(event, **data):
            self.note(text, level)
        after = self.tracker.stage
        if event == "convert":
            t = self.tracker
            why = ", ".join(f"{n} {kind}" for kind, n in sorted(t.rebuilt.items(), key=lambda kv: -kv[1])[:3])
            self.states["build"] = (
                "running",
                f"{t.todo} to convert, {t.reused} reused"
                + (f" ({why})" if why else "")
                + f"; {t.jobs} at a time"
                + (", Idle, 2 workers at most (out/gentle)" if t.gentle else ""),
            )
        elif event == "verify":
            self.states["build"] = (
                "running",
                f"checking {len(self.tracker.verify)} reused cars by building them again",
            )
        if before == "build" and after == "pack":
            t = self.tracker
            self.stage("build", "done", f"{t.converted} converted, {t.reused} reused, {len(t.failures)} failed")
            self.stage("pack", "running", t.pack_detail())
        elif after == "pack" and self.states["pack"][0] == "running":
            self.states["pack"] = ("running", self.tracker.pack_detail())
        self.show_event(event, data)

    def show_event(self, event: str, data: dict) -> None:
        """Show one build event (each display draws it its own way)."""

    def summary(self, summary: Summary) -> None:
        """Show the closing summary."""


class PlainReporter(Reporter):
    """One line per change, ASCII only: for pipes, logs and old consoles."""

    def __init__(self, stream):
        super().__init__()
        self.stream = stream
        self.shown: dict[str, tuple[str, str]] = {}

    def line(self, text: str) -> None:
        """Write one line to the terminal, ASCII only."""
        self.stream.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        self.stream.flush()

    def show_stage(self, key: str) -> None:
        state, detail = self.states[key]
        label = dict(STAGES)[key]
        before = self.shown.get(key, ("pending", ""))
        self.shown[key] = (state, detail)
        if state == "running" and before[0] == "running":
            if detail and detail != before[1]:
                self.line(f"  {detail}")
        elif state == "running":
            self.line(f"[{key}] {label}..." + (f" {detail}" if detail else ""))
        elif state in ("done", "skipped", "failed"):
            self.line(f"[{key}] {state}" + (f": {detail}" if detail else ""))

    def note(self, text: str, level: str = "info") -> None:
        prefix = {"error": "  FAILED ", "warn": "  warning: "}.get(level, "  ")
        self.line(prefix + text)

    def show_event(self, event: str, data: dict) -> None:
        t = self.tracker
        if event == "convert":
            mode = "Idle priority, 2 workers at most (out/gentle)" if t.gentle else "Below Normal priority"
            self.line(f"  {t.total} cars: {t.reused} reused, {t.todo} to convert, {t.jobs} at a time ({mode})")
        elif event == "car_start" and not data.get("verify"):
            self.line(f"  start {data['media']}")
        elif event == "car_done" and data["media"] not in t.verify:
            report = data.get("report") or {}
            state = "ok" if report.get("ok") else "failed"
            rate = t.rate()
            self.line(
                f"  done  {data['media']} {state} ({report.get('seconds') or 0:.0f} s)  "
                f"{t.converted}/{t.todo}" + (f", {rate:.1f}/min" if rate else "") + f", ETA {clock_text(t.eta())}"
            )
        elif event == "verify":
            self.line(f"  checking {len(t.verify)} reused cars by building them again")
        elif event in ("merge", "lint", "gui", "pack_reused"):
            self.line(f"  {t.pack_detail()}")

    def summary(self, s: Summary) -> None:
        for text in summary_lines(s):
            self.line(text)


def summary_lines(s: Summary) -> list[str]:
    """The closing summary as plain text lines (PlainReporter)."""
    lines = ["", f"Finished in {clock_text(s.elapsed)}."]
    if s.build_dir is not None:
        lines.append(
            f"  Cars:      {s.in_archive} in the archive ({s.built} built, {s.reused} reused), {len(s.failed)} failed"
        )
        lines.append(f"  Archive:   {archive_text(s)}")
    for f in s.failed:
        lines.append(f"    {f.media}: {f.cause}" + (f" - {f.error[:100]}" if f.error else ""))
        if f.log:
            lines.append(f"      log {f.log}")
    if s.installed:
        lines.append(
            f"  Installed: into {s.game}: {len(s.replaced)} files replaced (originals in dsfcars-backup), "
            f"{len(s.created)} added"
        )
        if s.save_backup:
            lines.append(f"  Save:      backed up to {s.save_backup}")
        lines.append(f"  Uninstall: {s.uninstall}")
    lines += [f"  Note: {n}" for n in s.notes]
    if s.log:
        lines.append(f"  Log:       {s.log}")
    return lines


class LiveReporter(Reporter):
    """rich: the stage list, the per-car bar and the running workers, redrawn in place."""

    ICONS: ClassVar[dict[str, str]] = {
        "done": "[green]✓[/]",
        "skipped": "[dim]-[/]",
        "failed": "[red]✗[/]",
        "pending": "[dim]·[/]",
    }

    def __init__(self, console, term_fd: int | None = None):
        super().__init__()
        from rich.live import Live
        from rich.spinner import Spinner

        self.console = console
        self.term_fd = term_fd
        self.spinner = Spinner("dots", style="cyan")
        self.live = Live(
            get_renderable=self.render,
            console=console,
            refresh_per_second=4,
            transient=False,
            redirect_stdout=False,
            redirect_stderr=False,
        )

    def __enter__(self):
        self.live.start()
        return self

    def __exit__(self, *exc):
        self.live.stop()
        return False

    def note(self, text: str, level: str = "info") -> None:
        from rich.text import Text

        style = {"error": "red", "warn": "yellow"}.get(level, "")
        mark = {"error": "✗ ", "warn": "! "}.get(level, "  ")
        self.console.print(Text(mark + text, style=style))

    def render(self):
        """The live display: the stages, and while cars convert the progress bar and the
        running workers."""
        from rich.console import Group
        from rich.progress_bar import ProgressBar
        from rich.table import Table

        if self.term_fd is not None:  # follow the window's size (fd 1 is the log file now)
            with contextlib.suppress(OSError, ValueError):
                size = os.get_terminal_size(self.term_fd)
                self.console.width, self.console.height = size.columns, size.lines
        stages = Table.grid(padding=(0, 1))
        stages.add_column(width=1)
        stages.add_column(style="bold", min_width=11)
        stages.add_column(overflow="ellipsis", no_wrap=True)
        stages.add_column(justify="right", style="dim")
        now = time.monotonic()
        for key, label in STAGES:
            state, detail = self.states[key]
            icon = self.spinner if state == "running" else self.ICONS.get(state, " ")
            elapsed = clock_text(now - self.started[key]) if state == "running" and key in self.started else ""
            stages.add_row(icon, label, f"[dim]{detail}[/]" if state != "running" else detail, elapsed)
        parts = [stages]
        t = self.tracker
        if self.states["build"][0] == "running" and t.phase in ("convert", "verify"):
            rate = t.rate()
            bar = Table.grid(padding=(0, 1))
            bar.add_column(style="bold")
            bar.add_column(width=max(10, min(40, self.console.width - 70)))
            bar.add_column(no_wrap=True)
            stats = f"{t.done}/{t.total} cars"
            stats += f" · {t.reused} reused" if t.reused else ""
            stats += f" · {rate:.1f}/min" if rate else ""
            stats += f" · ETA {clock_text(t.eta())}" if t.phase == "convert" else " · checking reused cars"
            stats += f" · [red]{len(t.failures)} failed[/]" if t.failures else ""
            bar.add_row(
                " Cars",
                ProgressBar(total=max(1, t.total), completed=t.done, complete_style="green", finished_style="green"),
                stats,
            )
            parts.append(bar)
            workers = Table.grid(padding=(0, 1))
            workers.add_column(width=3)
            workers.add_column(no_wrap=True)
            workers.add_column(justify="right", style="dim")
            workers.add_column(style="yellow", no_wrap=True, overflow="ellipsis")
            running = sorted(t.running.items(), key=lambda kv: kv[1]["started"])
            room = max(3, self.console.height - len(STAGES) - 4)  # a small window: the oldest only
            for media, info in running[:room]:
                workers.add_row("  ↳", media, clock_text(now - info["started"]), info["note"])
            queued = t.todo - t.converted - sum(1 for i in t.running.values() if not i["verify"])
            more = f"{len(running) - room} more running, " if len(running) > room else ""
            if queued > 0 or more:
                workers.add_row("", f"[dim]{more}{max(0, queued)} queued, {t.jobs} at a time[/]", "", "")
            parts.append(workers)
        return Group(*parts)

    def summary(self, s: Summary) -> None:
        from rich.table import Table
        from rich.text import Text

        self.console.print()
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold")
        grid.add_column()
        if s.build_dir is not None:
            failed = f", [red]{len(s.failed)} failed[/]" if s.failed else ", 0 failed"
            grid.add_row("Cars", f"{s.in_archive} in the archive ({s.built} built, {s.reused} reused){failed}")
            grid.add_row("Archive", archive_text(s))
        if s.installed:
            grid.add_row(
                "Installed",
                f"into {s.game}: {len(s.replaced)} files replaced (originals in dsfcars-backup), "
                f"{len(s.created)} added",
            )
            if s.save_backup:
                grid.add_row("Save backup", str(s.save_backup))
            grid.add_row("Uninstall", s.uninstall)
        for n in s.notes:
            grid.add_row("[yellow]Note[/]", n)
        if s.log:
            grid.add_row("Log", str(s.log))
        self.console.print(Text(f"Finished in {clock_text(s.elapsed)}", style="bold green"))
        self.console.print(grid)
        if s.failed:
            table = Table(title="Failed cars", title_justify="left", show_edge=False, header_style="bold red")
            table.add_column("car")
            table.add_column("cause")
            table.add_column("log", overflow="fold")
            for f in s.failed:
                table.add_row(f.media, f.cause + (f": {f.error[:80]}" if f.error else ""), f.log or "")
            self.console.print(table)


# --- the terminal ---------------------------------------------------------------------------------


def enable_vt(fd: int) -> bool:
    """ANSI escape sequences on the console behind fd (Windows 10+); False when it can't."""
    if os.name != "nt":
        return True
    try:
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(fd)
        mode = wintypes.DWORD()
        k32 = ctypes.windll.kernel32
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except (OSError, AttributeError, ValueError):
        return False


def make_reporter(term, term_fd: int, *, plain: bool) -> Reporter:
    """The live display (rich) on a terminal that takes ANSI escape sequences, else plain
    lines (also with --plain)."""
    if plain or not term.isatty() or not enable_vt(term_fd):
        return PlainReporter(term)
    from rich.console import Console

    width = 100
    with contextlib.suppress(OSError):
        width = os.get_terminal_size(term_fd).columns
    console = Console(file=term, force_terminal=True, legacy_windows=False, width=width, highlight=False)
    return LiveReporter(console, term_fd)
