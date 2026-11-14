"""Port many Forza cars into one dngvehicles.sp (batch mode for src/dsfforza/build/port_car.py).

    uv run dsff port_batch plan  [selection]            # what would be built, with hosts
    uv run dsff port_batch build --out out/batch/b1 [selection] [--jobs 6] [--gui] \\
        [--port "--mdxn-limit 0x2C0000 --damage"]

Selection (combinable; a list keeps its order, filters keep roster order):

    --car NAME[@fh1|@fm4] ...   --list FILE (one car per line, # comments)
    --game fh1|fm4              only cars taken from that game (FH1 is preferred when on both)
    --make NIS,BMW,Ford         Forza make code or make name
    --body sedan,hatch,...      host_select body class: sedan coupe convertible hatch supercar
                                muscle suv pickup race (race cars are left out unless named)
    --year 1960-1979            --no-counterpart / --counterpart (a vanilla DSF car of the same model)
    --exclude NAME ...          --diverse N (round-robin over body classes, N cars)  --max N

Every car is built by port_car.build_car in its own worker process (Below
Normal priority, at most --jobs at a time, a new one only while the job pool's RAM
budget has its peak, WORKER_RAM; jobpool.py), which writes the car's three VEHC entries to work/<media>.chnk and
a report. The merge then registers them into one copy of the vanilla archive
(SSLP/VSCT records), checks the root entry limit (--root-limit, default 1100:
150 vanilla cars plus at most 215 new ones; dsfcars.asi's archive module lifts
it, research/spool-limits.md) and UID clashes (a car that clashes is left out
and the rest merged again), writes the archive, and lints every car.
A car that fails in src/dsfforza/convert/damage/ is retried once without --damage, one
that fails in the cockpit step once with --no-cockpit (the host's interior),
and a worker that times out or dies without a report is not retried; a car
that still fails is left out and listed with its cause (the innermost
tools module and exception), and the rest still ship. Extractions are cached
in out/cache/<game>/<media>/ (port_car), and --resume reuses every car an
earlier run in the same --out built with the same ID and options.
--reuse DIR copies each car an earlier build made from the same job, input files
and code (work/<media>.fp.json, src/dsfforza/build/fingerprint.py) and converts
only the rest (repeat it for several builds; the first match wins); --verify-reuse N (default 2) converts N of the reused cars again and
stops the build if any differs; --rebuild-where EXPR and --trust-code let a fix
name the cars it affects (research/batch.md, "Selective rebuild").
Every build also takes cars from the car store all worktrees share and puts its own in
(src/dsfforza/build/carstore.py; --no-store: neither). `port_batch affected` lists the
cars a change since --base touches, from the store's fingerprints; build --affected builds
only those, build --before takes them from the store as --base made them, and --roster is
dsff install's selection and IDs (research/batch.md, "Which cars a change affects").

ID policy (research/batch.md): the garage lists model IDs 0..303 only
(research/gui-assets.md) and vanilla cars use 62..303, so the first cars of a
batch get IDs 1..61 (garage-visible), the rest 304 upwards (traffic, Shift and
scripts, but never the garage). --id-policy high puts every car at 304+
(the range confirmed in game); --first-id moves the high range.
--id-policy asi is for installs with dsfcars.asi (tools/asi, research/asi.md):
its garage module lists IDs up to 999 in up to 120 maker groups and keeps the
save flags of IDs 304+ in a side file, so every car can be in the garage. Cars
get every free ID in order: 1..61, the 92 IDs in 62..303 the vanilla archive
leaves unused, then --first-id up; cars that won't fit a maker group get a note.

Outputs in --out DIR: dngvehicles.sp (only when it fits in one CHNK, below 4 GiB,
and without --no-archive; else dngvehicles.plan.json, the merge that lint reads in
memory: research/batch.md, "Builds past 4 GiB"), dngvehicles.json (port_car's manifest
format, plus "batch": failures and timings), dngvehicles.lua (the
dsfcars\\ported.lua for register.lua), batch-report.json (per car its
engine sound: Forza source banks, loops used, pitch ranges, SBAK size, or why
it kept the host's), sound/<media>/ (sweep_new.wav and sweep_template.wav
previews, wav/ loops, report.json; src/dsfforza/convert/engine_sound.py) and, with --gui,
gui/ (gui.sp, Fonts.fchunk, hud.lua from src/dsfforza/game/gui_assets.py; when that step
fails, a gui/ already there is moved to gui-stale-<time>/, since it doesn't
match the new archive and install.ps1 would install it). A car whose
sound step crashes is retried once with --no-forza-sound. Every
port_car option is accepted inside --port "..." (e.g. --damage, --host 266,
--mdxn-limit 0x2C0000).

The game folder is read from --dsf (DIR/Install/dngvehicles.sp) and passed to
the other tools as DSF_GAME_DIR; derived data the tools need (vehiclestats.lua,
gamedb.slt) is made by src/dsfforza/game/game_data.py prepare when missing.
"""

from __future__ import annotations

import argparse
import bisect
import contextlib
import copy
import ctypes
import datetime
import hashlib
import json
import os
import shlex
import struct
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

from dsfforza import REPO, safe_stdio
from dsfforza.build import forza_roster, gentle
from dsfforza.formats.chnk import ENTRY_LIMIT
from dsfforza.game import game_paths

GARAGE_IDS = range(1, 62)  # garage loop covers 0..303; 0 is left alone, vanilla starts at 62
HIGH_FIRST_ID = 304
WORKER_TIMEOUT = 45 * 60
# Each job kind's RAM in the job pool (jobpool.py): what its slot reserves while held. The kind's
# highest measured peak private bytes (the kernel's PeakPagefileUsage over the job's process tree),
# rounded up to 100 MiB (research/batch.md, "Job pool", "RAM measured", 2026-11-14).
WORKER_RAM = 3300 << 20  # a conversion worker: 3264 MiB VIP_Viper_13, 3.0 GiB AST_V12Zagato_12; most cars 0.9-1.1
RENDER_RAM = 1700 << 20  # a garage-picture render process: 1650 MiB over the 573-car archive
LINT_RAM = 3100 << 20  # an upvh lint-many process: 3036 MiB merging the 721-car roster's plan in memory
DIFF_RAM = 2900 << 20  # a render_compare diff process, one car each: 2805 MiB over 80 cars (one drawing 20: 3381)
COMPARE_RAM = 1200 << 20  # a render_compare run process: 1123 MiB on the roster build (4 large cars)
RESCORE_RAM = 800 << 20  # a render_compare rescore process: 736 MiB
MEMO_RAM = 1200 << 20  # a door-count memo process (fingerprint.Memo.fill): 1153 MiB, the roster's 716 cars
COCKPIT_SURVEY_RAM = 800 << 20  # a cockpit_survey process: 711 MiB (the six largest cars and 8 others)
PLATES_SURVEY_RAM = 1200 << 20  # a plates_survey process: 1172 MiB (12 cars, the largest among them)
BELOW_NORMAL = 0x00004000
DEFAULT_PORT_OPTIONS = "--mdxn-limit 0x2C0000 --damage"


# --- environment ------------------------------------------------------------------------


def game_from_dsf(dsf: Path) -> Path:
    """The game folder of a vanilla archive path (<game>/Install/dngvehicles.sp): one up
    from a dsfcars backup's copy, and the real game (game_paths.game_dir) for the repo's
    vanilla copy."""
    game = dsf.resolve().parent.parent
    # <game>/dsfcars-backup/Install/dngvehicles.sp (src/dsfforza/game/game_paths.py): the game is one up.
    if game == game_paths.REPO_VANILLA.resolve():  # the repo's vanilla copy (a junction in a worktree): the real game
        return game_paths.game_dir()
    return game.parent if game.name in game_paths.BACKUPS else game


def set_game_env(dsf: Path) -> None:
    """Point every tool at this game folder (host_select, gui_assets read DSF_GAME_DIR
    when imported, so call this before importing them)."""
    os.environ["DSF_GAME_DIR"] = str(game_from_dsf(dsf))


def free_ram() -> int | None:
    """Bytes of RAM free now (None outside Windows)."""
    return _memory_status("avail")


def total_ram() -> int | None:
    """Bytes of RAM in the PC (None outside Windows)."""
    return _memory_status("total")


def default_jobs(cpus: int | None = None, ram: int | None = None) -> int:
    """Worker processes: one per logical CPU, as many as WORKER_RAM each fits in the PC's
    RAM. More than one per core still helps: 32 cars converted in 374 s with 8 workers,
    345 s with 12 and 323 s with 16 on an 8-core, 16-thread PC (2026-10-30). With the job
    pool on, a new worker also waits until its RAM budget has WORKER_RAM (jobpool.budget),
    so other programs' and the test VMs' memory is left alone."""
    cpus = cpus if cpus is not None else os.cpu_count()
    ram = ram if ram is not None else total_ram()
    jobs = max(1, cpus or 2)
    return max(1, min(jobs, ram // WORKER_RAM)) if ram else jobs


def render_jobs(jobs: int, free: int | None = None) -> int:
    """Garage-picture render processes for `jobs`: at most as many as RENDER_RAM each fits in
    the RAM free when the gui step starts."""
    free = free if free is not None else free_ram()
    return max(1, min(jobs, free // RENDER_RAM)) if free else max(1, jobs)


def lint_jobs(jobs: int, free: int | None = None) -> int:
    """upvh lint-many processes for `jobs`: at most as many as LINT_RAM each fits in the RAM
    free when the lint starts. Each reads the whole archive first, so more processes add
    that once more each, but the cars' lints spread: the 573 cars of a full build linted in
    73 s with 4 processes, 42 s with 9 and 39 s with 16 (2026-10-30)."""
    free = free if free is not None else free_ram()
    return max(1, min(jobs, free // LINT_RAM)) if free else max(1, jobs)


def _memory_status(name: str) -> int | None:
    """A field of GlobalMemoryStatusEx ("avail", "total": bytes), or None outside Windows."""
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            ("total", ctypes.c_ulonglong),
            ("avail", ctypes.c_ulonglong),
            ("page_total", ctypes.c_ulonglong),
            ("page_avail", ctypes.c_ulonglong),
            ("virt_total", ctypes.c_ulonglong),
            ("virt_avail", ctypes.c_ulonglong),
            ("ext", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return getattr(status, name)


def lower_priority() -> None:
    """This process at Below Normal (workers are started that way too)."""
    if os.name == "nt":
        gentle.set_priority(BELOW_NORMAL)
    else:
        os.nice(5)
    gentle.apply()  # out/gentle: Idle, held for the children too (src/dsfforza/build/gentle.py)


def emit(progress, event: str, **data) -> None:
    """Report a build event to `progress` (args.progress, set by a caller such as
    src/dsfforza/build/install.py; None from the command line). Events: plan, convert,
    car_start, car_retry, car_done, verify, merge, lint, gui, gui_log, packages,
    packages_failed, done."""
    if progress is not None:
        progress(event, **data)


def port_car_args(argv: list[str]) -> argparse.Namespace:
    """port_car.py's own options parsed from argv (so the batch takes all of them
    and their defaults)."""
    from dsfforza.build import port_car

    return port_car.build_parser().parse_args(argv)


# --- the roster and selection ------------------------------------------------------------


@dataclass
class Pick:
    """One car of a selection, as plan prints it and build converts it: the car and its
    Forza source, body, drive and power, the best host and the vanilla counterpart, the
    spool ID assign_ids gives it, and notes (a car with notes isn't built)."""

    media: str
    game: str
    year: int | None
    games: str
    source: dict
    make: str = ""
    body: str = ""
    drive: str | None = None
    power_kw: float | None = None
    host: int | None = None
    host_model: str = ""
    host_score: float = 0.0
    counterpart: int | None = None  # vanilla car of the same model (identity 60)
    counterpart_model: str = ""
    spool_id: int | None = None
    host_guess: bool = False  # car not extracted yet: doors from the body class, the build may pick another host
    notes: list[str] = field(default_factory=list)
    notes_soft: str = ""  # informational (e.g. why no garage ID)


def load_roster(isos: list[Path]):
    """The cars of the disc images and of the FM4 DLC packs under assets/ (fm4_dlc.packages:
    none with --no-fm4-dlc)."""
    from dsfforza.build import fm4_dlc
    from dsfforza.build.forza_roster import roster

    cars, _ = roster(isos, fm4_dlc.packages())
    return {k: c for k, c in cars.items() if c.kind == "car"}


DLC_LIST = REPO / "tools" / "batches" / "fm4-dlc.txt"  # the DLC cars no disc has, by pack


def dlc_requests(cars: dict, path: Path = DLC_LIST) -> list[tuple[str, str | None]]:
    """--fm4-dlc-cars: the cars of `path` (tools/batches/fm4-dlc.txt) that the roster has from
    a DLC pack found, in the list's order (a pack the user lacks adds nothing)."""
    out = []
    for media, game in read_list(path):
        car = cars.get(media.lower())
        if car is not None and any(s.dlc and s.kind == "car" for s in car.sources):
            out.append((car.name, game))
    return out


def gamedbs(isos: list[Path], cache: Path) -> dict[str, Path]:
    """{game: gamedb} for the plan's lookups by media name: FM4's with the DLC packs' rows when
    they are used (fm4_dlc.gamedb; it holds every disc row as it is)."""
    from dsfforza.build import fm4_dlc

    out = {}
    for game in ("FH1", "FM4"):
        db = fm4_dlc.gamedb(isos, game, cache)
        if db:
            out[game] = db
    return out


def describe_cars(
    cars: dict, requests: list[tuple[str, str | None]], dsf: Path, dbs: dict[str, Path], cache: Path | None = None
) -> list[Pick]:
    """A Pick per (media, game or None) with body class, best host and the vanilla
    counterpart; unknown names become picks with a note. The host is picked as
    port_car.pick_hosts picks it: the source game's gamedb and the car's extracted
    folder under `cache` (its carbin's door count)."""
    from dsfforza.build import host_select as hs

    hosts = hs.survey(dsf)
    memo = hs.door_memo()
    try:
        _fill_doors(cars, requests, dbs, cache, memo)
        return _describe(cars, requests, dbs, cache, hosts, memo)
    finally:
        memo.save()


DOOR_FILL_MIN = 16  # door counts to measure before they are worth a process pool


def _fill_doors(cars: dict, requests, dbs: dict[str, Path], cache: Path | None, memo) -> None:
    """Measure the door counts _describe will ask the memo for and it doesn't hold (a
    full roster's take minutes one after the other: after any change to the carbin
    reader or the door rule) in a process pool first (fingerprint.Memo.fill), so the
    loop finds them. The keys and bodies are forza_info's."""
    from dsfforza.build import host_select as hs
    from dsfforza.build.port_car import CarRequest, car_folder, pick_source

    if cache is None:
        return
    tasks = []
    for media, game in requests:
        car = cars.get(media.lower())
        if car is None:
            continue
        try:
            rcar, source = pick_source(cars, CarRequest(car.name, game))
        except SystemExit:
            continue
        folder = car_folder(cache, source.game, rcar.name)
        if not folder.is_dir():
            continue
        db = dbs.get(source.game)
        body = hs.forza_info(rcar.name, {source.game: db} if db else None, source.game).body
        if body in hs.DOOR_RULE_BODIES:
            key = hs.door_key(folder, rcar.name, body)
            tasks.append((key, "dsfforza.build.host_select:_carbin_doors", (folder, rcar.name, body)))
    tasks = [t for t in {t[0]: t for t in tasks}.values() if not memo.valid(t[0])]
    if len(tasks) >= DOOR_FILL_MIN:
        print(f"measuring {len(tasks)} cars' door counts (their carbins or the door code changed)")
        memo.fill(tasks, gentle.jobs(os.cpu_count() or 2))


def _describe(cars: dict, requests, dbs: dict[str, Path], cache: Path | None, hosts: list, memo) -> list[Pick]:
    from dsfforza.build import host_select as hs
    from dsfforza.build.port_car import CarRequest, car_folder, pick_source

    picks = []
    for media, game in requests:
        car = cars.get(media.lower())
        if car is None:
            picks.append(Pick(media, game or "?", None, "", {}, notes=["not a car on the discs or DLC packs found"]))
            continue
        try:
            rcar, source = pick_source(cars, CarRequest(car.name, game))
        except SystemExit as error:
            picks.append(Pick(car.name, game or "?", car.year, car.games, {}, notes=[str(error)]))
            continue
        folder = car_folder(cache, source.game, rcar.name) if cache is not None else None
        extracted = folder is not None and folder.is_dir()
        db = dbs.get(source.game)
        info = hs.forza_info(
            rcar.name, {source.game: db} if db else None, source.game, folder if extracted else None, memo
        )
        ranking = hs.rank(info, hosts)
        best = ranking[0]
        counterpart = None
        for h in hosts:
            score, _ = hs.identity_score(h, info)
            if score >= 60 and not h["problems"]:
                counterpart = h
                break
        picks.append(
            Pick(
                rcar.name,
                source.game,
                rcar.year,
                rcar.games,
                asdict(source),
                make=info.make,
                body=info.body,
                drive=info.drive,
                power_kw=info.engine.power_kw,
                host=None if best.excluded else best.id,
                host_model=best.host["model"],
                host_score=best.score,
                counterpart=counterpart["id"] if counterpart else None,
                counterpart_model=counterpart["model"] if counterpart else "",
                host_guess=not extracted and info.body in hs.DOOR_RULE_BODIES,
                notes=[] if not best.excluded else [f"no usable host ({best.excluded})"],
            )
        )
    return picks


def read_list(path: Path) -> list[tuple[str, str | None]]:
    """The (media, game or None) lines of a car list file: one car per line, MEDIA or
    MEDIA@fh1/@fm4, # starts a comment."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            media, _, game = line.split()[0].partition("@")
            out.append((media, game.upper() or None))
    return out


def select(args, cars: dict, dsf: Path, dbs: dict[str, Path]) -> list[Pick]:
    """The cars a plan/affected/build selection names, described (describe_cars): the
    named ones in their order, else the roster cars the filters keep; then --exclude,
    --diverse and --max, each car once."""
    requests, named = selection_requests(args, cars)
    picks = describe_cars(cars, requests, dsf, dbs, args.cache)
    excluded = {e.lower() for e in args.exclude or []}
    keep = (lambda _: True) if named else roster_filter(args)  # a named list is taken as given
    out = [p for p in picks if p.media.lower() not in excluded and keep(p)]
    if args.diverse:
        out = diverse(out, args.diverse)
    if args.max:
        out = out[: args.max]
    return unique_cars(out)


def selection_requests(args, cars: dict) -> tuple[list[tuple[str, str | None]], bool]:
    """(the (media, game or None) requests of a selection, whether they were named): --car,
    --list and --fm4-dlc-cars in that order, else every roster car (of --game) by name."""
    named = [(m, g.upper() or None) for m, _, g in (c.partition("@") for c in args.car or [])]
    for path in args.list or []:
        named += read_list(path)
    if getattr(args, "fm4_dlc_cars", False):
        named += dlc_requests(cars)
    if named:
        return named, True
    game = args.game.upper() if args.game else None
    roster_cars = sorted(cars.values(), key=lambda c: c.name.lower())
    return [(c.name, game) for c in roster_cars if not game or game in c.games.split(",")], False


def roster_filter(args):
    """A predicate over Picks for a roster selection's filters: no notes, --game, --make (a
    make code or name), --body (race cars only when named), --year, --counterpart and
    --no-counterpart."""
    game = args.game.upper() if args.game else None
    makes = {m.strip().lower() for m in (args.make or "").split(",") if m.strip()}
    bodies = {b.strip().lower() for b in (args.body or "").split(",") if b.strip()}
    years = None
    if args.year:
        lo, _, hi = args.year.partition("-")
        years = (int(lo or 0), int(hi or 9999))

    def keep(p: Pick) -> bool:
        if p.notes or (game and p.game != game):
            return False
        if makes and p.make.lower() not in makes and p.media.split("_")[0].lower() not in makes:
            return False
        if (bodies and p.body not in bodies) or (not bodies and p.body == "race"):
            return False
        if years and not (p.year and years[0] <= p.year <= years[1]):
            return False
        if args.no_counterpart and p.counterpart is not None:
            return False
        return not (args.counterpart and p.counterpart is None)

    return keep


def unique_cars(picks: list[Pick]) -> list[Pick]:
    """The picks without repeats of a car (by media name, case-blind), first one kept."""
    seen = set()
    out = []
    for p in picks:
        if p.media.lower() not in seen:
            seen.add(p.media.lower())
            out.append(p)
    return out


def diverse(picks: list[Pick], count: int) -> list[Pick]:
    """Round-robin over body classes, each class ordered by a stable hash, so the
    pick spreads over classes, makes and eras without being random per run."""
    groups: dict[str, list[Pick]] = {}
    for p in sorted(picks, key=lambda p: hashlib.sha1(p.media.encode()).hexdigest()):
        groups.setdefault(p.body, []).append(p)
    out, makes = [], set()
    while len(out) < count and any(groups.values()):
        for body in sorted(groups):
            if groups[body] and len(out) < count:
                # prefer a make not taken yet
                bucket = groups[body]
                index = next((i for i, p in enumerate(bucket) if p.make not in makes), 0)
                p = bucket.pop(index)
                makes.add(p.make)
                out.append(p)
    return out


# Garage limits (research/batch.md, confirmed-static): the maker list holds at most
# 40 groups (vanilla: 35 selectable) and 20 cars per group (vanilla: Chevrolet 16), with no
# capacity check, so a garage ID is only given while its maker group has room. The
# margin leaves space for mods that make more vanilla cars selectable (Felony Rework);
# register.lua checks the real table again at runtime.
GARAGE_GROUP_CAP = 20 - 1
GARAGE_GROUPS_CAP = 40 - 1
ASI_GROUPS_CAP = 120 - 1  # dsfcars.asi [garage] max_groups (default 120)
ASI_GROUP_CAP = 1000  # dsfcars.asi [garage] max_cars: a maker group holds all its cars
# --id-policy asi: the low IDs first. 62..303 holds 150 vanilla cars (and spool IDs without
# a vehicleStats entry: 123, 131, 277, 289); the 92 others appear in no traffic set,
# vehicleStats key or model-ID field of the game's Lua (research/asi.md), and the
# archive's own IDs are skipped through `taken` anyway.
ASI_LOW_IDS = list(range(1, 304))
RESERVED_IDS = {622, 623}  # Felony Rework's vehicleStats dummies (not models)


def garage_groups() -> tuple[dict[str, int], dict[str, str]]:
    """({group key: vanilla selectable cars}, {DSF maker key: group key}) from the vanilla
    vehicleStats. The group key is the ManufacturerName string ("ID:242753" = Chevrolet),
    the maker key what gui_assets.dsf_make returns ("CHEVROLET")."""
    try:
        from dsfforza.game import gui_assets

        stats = gui_assets.vanilla_stats()
        makes, _ = gui_assets.vanilla_codes(gui_assets.read_strings())
    except (OSError, ValueError, KeyError):
        return {}, {}
    counts: dict[str, int] = {}
    for sid, fields in stats.items():
        if fields.get("Selectable") == "true" and sid <= 303:
            key = fields.get("ManufacturerName", "")
            counts[key] = counts.get(key, 0) + 1
    return counts, {make: name for make, (_, name) in makes.items()}


def car_groups(picks: list[Pick], group_of: dict[str, str], isos: list[Path] | None = None) -> dict[str, str]:
    """{media: garage group key} as gui_assets.build will name the maker: Forza's own
    maker name (src/dsfforza/formats/forza_strings.py via gui_assets.car_texts) mapped by
    gui_assets.dsf_make to a vanilla maker's "ID:nnnnnn" string, else the plain name."""
    from dsfforza.game import gui_assets

    texts = gui_assets.car_texts([{"media": p.media, "game": p.game} for p in picks], isos)
    out = {}
    for p in picks:
        t = texts[p.media]
        key = gui_assets.dsf_make(t["make"], t["model"])
        out[p.media] = group_of.get(key, key)
    return out


# register.lua: makes without a garage group of their own share these. A plain name: the garage
# files cars by ManufacturerName and builds its groups at each game start (FUN_006C57D0); the save
# keeps flags per model ID, not per group (research/asi.md, "Garage list").
OVERFLOW_GROUP = "ADDED CARS"


def garage_group(group: str, counts: dict[str, int], cap: int, groups_cap: int) -> str | None:
    """The garage maker group a car joins, or None: its own group while it has room (a new
    make opens one while fewer than groups_cap exist), else a shared ADDED CARS, ADDED
    CARS 2 ... group (the same rule as register.lua's garageGroup). One group slot is kept for it."""
    if group in counts:
        if counts[group] < cap:
            return group
    elif len(counts) + (0 if OVERFLOW_GROUP in counts else 1) < groups_cap:  # keep room for the shared group
        return group
    n = 1
    while True:
        name = OVERFLOW_GROUP if n == 1 else f"{OVERFLOW_GROUP} {n}"
        if name in counts and counts[name] < cap:
            return name
        if name not in counts:
            return name if len(counts) < groups_cap else None
        n += 1


def root_limit_of(args) -> int:
    """--root-limit: 1100 (vanilla) unless the install uses dsfcars.asi's archive module."""
    limit = getattr(args, "root_limit", None) or ENTRY_LIMIT
    if limit > ENTRY_LIMIT:
        print(
            f"note: root limit {limit}: the archive needs dsfcars.asi with [archive] enabled "
            f"(the vanilla game reads {ENTRY_LIMIT} entries)",
            file=sys.stderr,
        )
    return limit


def assign_ids(
    picks: list[Pick], taken: set[int], policy: str, first_high: int, isos: list[Path] | None = None
) -> None:
    """policy "garage": IDs 1..61 (listed in the garage) while a maker group has room
    for the car, else first_high and up; "high": first_high and up only; "asi"
    (installs with dsfcars.asi): every free ID below 1000 in order, 1..61, the 92
    IDs in 62..303 the vanilla archive doesn't use, then first_high up, each car
    garage-listed by the plugin while the maker list (ASI_GROUPS_CAP groups) has
    room; the plugin's groups take any number of cars. List order decides which cars get the garage IDs (garage) or the group
    slots (asi). IDs some mod is known to use (RESERVED_IDS) are skipped."""
    low = [i for i in GARAGE_IDS if i not in taken] if policy == "garage" else []
    asi = [i for i in ASI_LOW_IDS if i not in taken and i not in RESERVED_IDS] if policy == "asi" else []
    counts, group_of = garage_groups() if (low or policy == "asi") else ({}, {})
    groups = car_groups(picks, group_of, isos) if (low or policy == "asi") else {}
    next_id = first_high
    for p in picks:
        own = groups.get(p.media)
        group = garage_group(own, counts, GARAGE_GROUP_CAP, GARAGE_GROUPS_CAP) if low else None
        if group is not None:
            p.spool_id = low.pop(0)
            counts[group] = counts.get(group, 0) + 1
            if group != own:
                p.notes_soft = f"garage group {group}"
            continue
        if low:
            p.notes_soft = "no garage group left"
        if policy == "asi" and own is not None:
            group = garage_group(own, counts, ASI_GROUP_CAP, ASI_GROUPS_CAP)
            if group is None:
                p.notes_soft = "no garage group left (dsfcars.asi)"
            else:
                counts[group] = counts.get(group, 0) + 1
                if group != own:
                    p.notes_soft = f"garage group {group}"
        if asi:
            p.spool_id = asi.pop(0)
            taken.add(p.spool_id)
            continue
        while next_id in taken or next_id in RESERVED_IDS:
            next_id += 1
        if next_id >= 1000:
            raise SystemExit("no free base spool ID below 1000")
        p.spool_id = next_id
        taken.add(next_id)


# --- one car (worker process) -------------------------------------------------------------

CAUSES = (
    ("not a car on the discs", "not on the discs"),
    ("not on the", "not on the discs"),
    ("no main carbin", "extraction"),
    ("over the size limits", "size limits"),
    ("interior entry is over", "size limits"),
    ("no usable host", "no usable host"),
    ("UID counter out of range", "too many resources (UID counter)"),
    ("duplicate", "UID clash"),
    ("MemoryError", "out of memory"),
)


def cause_of(text: str) -> str:
    """A short failure cause: a known message, else "<dsfforza module>: <exception>"
    from the innermost frame in src/dsfforza/ of the traceback (a package's submodule counts
    as the package: convert/damage/plan.py is damage.py)."""
    import re

    for needle, cause in CAUSES:
        if needle in text:
            return cause
    frames = re.findall(r'dsfforza[\\/](?:\w+[\\/])?(\w+)(?:[\\/]\w+)?\.py", line \d+', text)
    frames = [f for f in frames if f not in ("vehicle_mesh", "chnk")] or frames  # the step, not a helper
    error = re.findall(r"^(\w+(?:Error|Exception|Exit)):", text, re.M)
    if frames:
        return f"{frames[-1]}.py: {error[-1] if error else 'error'}"
    return "conversion error"


def fallback_for(report: dict, options: list[str]) -> tuple[list[str], str] | None:
    """Options for one retry after a failure that a lesser build avoids: without
    --damage when src/dsfforza/convert/damage/ failed, with --no-cockpit (the host's interior)
    when src/dsfforza/convert/cockpit/ or the cockpit carbin failed. None: no retry, also for a
    worker that timed out or died without a report (its "error" is the log tail,
    which names every step the car got through, so it says nothing about the cause)."""
    if report.get("cause") in WORKER_DEATHS:
        return None
    import re

    text = (report.get("traceback") or "") + (report.get("error") or "")
    if re.search(r"damage(?:[\\/]\w+)?\.py", text) and "--damage" in options:
        out, skip = [], False
        for o in options:
            if skip:
                skip = False
                if o in ("panels", "hull"):
                    continue
            if o == "--damage":
                skip = True
                continue
            out.append(o)
        return out, "without --damage (src/dsfforza/convert/damage.py failed)"
    if ("cockpit" in text.lower()) and "--no-cockpit" not in options:
        return options + ["--no-cockpit"], "with the host's interior (--no-cockpit; the Forza cockpit failed)"
    if ("engine_sound.py" in text or "forza_audio.py" in text) and "--no-forza-sound" not in options:
        return options + ["--no-forza-sound"], "with the host's sound (--no-forza-sound; the Forza sound failed)"
    return None


def worker(job_path: Path) -> int:
    """Build one car: write work/<media>.chnk (the three VEHC entries) and
    work/<media>.json (report). Exit code 0 on success."""
    lower_priority()
    job = json.loads(job_path.read_text(encoding="utf-8"))
    os.environ["DSF_GAME_DIR"] = job["game_dir"]
    out_chunk, out_report = Path(job["chunk"]), Path(job["report"])
    recorder = None
    if job.get("fingerprint", True):  # what the car read and ran, for a later --reuse
        from dsfforza.build import fingerprint

        outputs = [out_chunk.parent] + ([Path(job["sound_dir"])] if job.get("sound_dir") else [])
        recorder = fingerprint.Recorder(outputs).start()
    started = time.time()
    lines: list[str] = []

    def log(text=""):
        print(text, flush=True)
        lines.append(str(text))

    report = {"media": job["media"], "game": job["game"], "spool_id": job["spool_id"], "ok": False}
    try:
        from dsfforza.build import clone_vehicle, port_car
        from dsfforza.build.forza_roster import Source
        from dsfforza.formats.chnk import read_root, write_chunk

        dsf = Path(job["dsf"])
        args = port_car_args(
            [
                "--dsf",
                str(dsf),
                "--out",
                str(out_chunk.with_suffix(".unused.sp")),
                "--car",
                f"{job['media']}@{job['game'].lower()}",
                "--cache",
                job["cache"],
            ]
            + job["port_options"]
        )
        source = Source(**job["source"])
        isos = [Path(p) for p in job["isos"]]
        prep = port_car.prepare_car(args, source, job["media"], isos, log)
        host, sound = prep.host, prep.sound
        with dsf.open("rb") as stream:
            # The one parse of the vanilla archive this worker makes: the dials' and paints'
            # lookups share it (chnk.read_root); the conversion copies what it changes.
            root = read_root(stream)
            entries = root.entries
            nonr_limit, mdxn_max = port_car.vanilla_limits(stream, entries, args.mdxn_limit)
            names = {s: entries[i].name for s, i, k in clone_vehicle.sslp_records(stream, entries) if k == 0}
            if job.get("sound_dir"):
                args.forza_sound_dir = Path(job["sound_dir"])
            args.probe_attempts = job.get("probe_attempts")
            clones, built = port_car.convert_car(
                stream, entries, prep, job["spool_id"], args, nonr_limit, mdxn_max, names, isos, log
            )
            temp = out_chunk.with_suffix(".tmp")
            with temp.open("wb") as out:
                write_chunk(out, stream, clones, root.version)
            Path(temp).replace(out_chunk)
        report.update(
            {
                "ok": True,
                "host": host,
                "host_name": names[host],
                "sound": sound,
                "entry": {
                    **port_car.car_entry(
                        job["media"],
                        job["year"],
                        job["games"],
                        source,
                        args.cache,
                        job["spool_id"],
                        host,
                        names[host],
                        built,
                    ),
                    **({"batch_fallback": job["fallback"]} if job.get("fallback") else {}),
                },
                "port_args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()},
            }
        )
    except BaseException as error:  # noqa: BLE001 - SystemExit from port_car included: the car's report holds it
        text = f"{type(error).__name__}: {error}"
        report.update(
            {
                "error": text,
                "cause": cause_of(text + "\n" + traceback.format_exc()),
                "traceback": traceback.format_exc()[-4000:],
            }
        )
        log(text)
    report["seconds"] = round(time.time() - started, 1)
    # Atomic: a worker killed while writing (timeout) must not leave half a report.
    temp = out_report.with_suffix(".json.tmp")
    temp.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    Path(temp).replace(out_report)
    if recorder is not None:
        try:
            fp = recorder.finish(job)
            temp = out_chunk.with_suffix(".fp.json.tmp")
            temp.write_text(json.dumps(fp, indent=1) + "\n", encoding="utf-8")
            temp.replace(out_chunk.with_suffix(".fp.json"))
        except Exception as error:  # noqa: BLE001 - logged; no fingerprint: a later --reuse builds the car again
            log(f"fingerprint: {type(error).__name__}: {error}")
    return 0 if report["ok"] else 1


# --- the pool ---------------------------------------------------------------------------------

WORKER_DEATHS = ("timeout", "worker crash")


def worker_report(work: Path, media: str, code: int | None) -> dict:
    """The report the worker wrote, or a failure with its log tail when it timed out
    (code None) or died without writing one (or left one that doesn't parse)."""
    report_path = work / f"{media}.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if isinstance(report, dict) and "ok" in report:
            return report
    except (OSError, ValueError):
        pass
    report_path.unlink(missing_ok=True)
    try:
        tail = (work / f"{media}.log").read_text(encoding="utf-8", errors="replace")[-1500:]
    except OSError:
        tail = ""
    return {
        "media": media,
        "ok": False,
        "cause": "timeout" if code is None else "worker crash",
        "error": f"exit code {code}; log tail: {tail}",
    }


def _fingerprint_worker_env() -> dict[str, str]:
    """fingerprint.WORKER_ENV: one BLAS thread per worker, so a conversion is repeatable."""
    from dsfforza.build import fingerprint

    return fingerprint.WORKER_ENV


def run_workers(jobs: list[dict], work: Path, parallel: int, log=print, progress=None) -> dict[str, dict]:
    """Run one worker process per job, at most `parallel` at once, Below Normal priority,
    each on a slot of the machine's job pool (src/dsfforza/build/jobpool.py: every build and
    survey on the PC shares it). A worker that can't start, times out or dies is a failed car, not a failed batch;
    workers still running when this exits early (an error, Ctrl+C) are stopped.
    `progress` gets car_start, car_retry and car_done events (emit)."""
    from dsfforza.build import jobpool

    pending = list(jobs)
    jobs_by_media = {j["media"]: j for j in jobs}
    running: dict[str, tuple[subprocess.Popen, float, object]] = {}
    slots: dict[str, jobpool.Slot] = {}  # each running worker's slot of the machine's job pool
    results: dict[str, dict] = {}
    flags = gentle.creation_flags(BELOW_NORMAL if os.name == "nt" else 0)
    waiting = False
    try:
        while pending or running:
            while pending and len(running) < parallel:
                # A slot of the job pool all builds and surveys share (jobpool.py), reserving
                # WORKER_RAM for the worker process started next (attach).
                slot = jobpool.try_acquire(pending[0]["media"], WORKER_RAM, child=True)
                if slot is None:
                    if not running and not waiting:
                        log("  job pool: every slot is busy; waiting (uv run dsff jobpool status)")
                    waiting = True
                    break
                waiting = False
                job = pending.pop(0)
                try:
                    proc, log_file = start_worker(job, work, flags)
                except OSError as error:
                    slot.release()
                    results[job["media"]] = start_failure(job, error)
                    log(f"  FAILED to start {job['media']}: {error}")
                    emit(progress, "car_done", media=job["media"], report=results[job["media"]], log=None)
                    continue
                jobpool.adopt(slot, proc)  # it ends with this process, its memory counts against the slot
                running[job["media"]] = (proc, time.time(), log_file)
                slots[job["media"]] = slot
                log(f"  start {job['media']} (ID {job['spool_id']}); {len(running)} running, {len(pending)} queued")
                emit(
                    progress,
                    "car_start",
                    media=job["media"],
                    spool_id=job["spool_id"],
                    fallback=job.get("fallback"),
                    verify=bool(job.get("verify")),
                )
            time.sleep(1.0)
            for media, (proc, started, log_file) in list(running.items()):
                code = proc.poll()
                if code is None and time.time() - started < WORKER_TIMEOUT:
                    continue
                if code is None:
                    proc.kill()
                    proc.wait()
                log_file.close()
                del running[media]
                slots.pop(media).release()
                job = jobs_by_media[media]
                report = finished_report(work, job, code, started)
                retry = retry_job(job, report, work)
                if retry is not None:
                    jobs_by_media[media] = retry
                    pending.append(retry)
                    log(f"  retry {media} {retry['fallback']}")
                    emit(progress, "car_retry", media=media, why=retry["fallback"], cause=report.get("cause"))
                    continue
                results[media] = report
                state = "ok" if report.get("ok") else f"FAILED ({report.get('cause')}: {report.get('error', '')[:160]})"
                log(
                    f"  done  {media} in {report['seconds']:.0f} s: {state}"
                    + (f" ({job['fallback']})" if job.get("fallback") else "")
                )
                emit(progress, "car_done", media=media, report=report, log=str(work / f"{media}.log"))
    finally:
        stop_workers(running, slots)  # only when leaving early: none run on otherwise
        jobpool.leave_queue()
    return results


def stop_workers(running: dict, slots: dict) -> None:
    """Stop run_workers' running workers ({media: (process, start time, log file)}) and give
    their job pool slots back."""
    for proc, _, log_file in running.values():
        proc.kill()
        proc.wait()
        log_file.close()
    for slot in slots.values():
        slot.release()


def start_worker(job: dict, work: Path, flags: int) -> tuple[subprocess.Popen, object]:
    """Start the worker process of one job: work/<media>.job.json is its job, work/<media>.log
    its output. Returns (the process, its open log file); OSError when it can't start."""
    job_path = work / f"{job['media']}.job.json"
    job_path.write_text(json.dumps(job, indent=1), encoding="utf-8")
    log_file = (work / f"{job['media']}.log").open("w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "dsfforza.build.port_batch", "_worker", str(job_path)],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            cwd=str(REPO),
            env={**os.environ, "PYTHONUNBUFFERED": "1", **_fingerprint_worker_env()},
        )
    except OSError:
        log_file.close()
        raise
    return proc, log_file


def start_failure(job: dict, error: OSError) -> dict:
    """The report of a car whose worker process could not start."""
    return {
        "media": job["media"],
        "spool_id": job["spool_id"],
        "ok": False,
        "cause": "worker crash",
        "error": f"could not start: {error}",
    }


def finished_report(work: Path, job: dict, code: int | None, started: float) -> dict:
    """The report of a job's worker that ended (code None: stopped at WORKER_TIMEOUT):
    worker_report, with the job's spool ID when it gives none, the seconds of every try of
    the car, and for a retry its fallback and the first try's error and cause."""
    report = worker_report(work, job["media"], code)
    report.setdefault("spool_id", job["spool_id"])
    report["seconds"] = round(time.time() - started, 1) + job.get("seconds_before", 0)
    if job.get("fallback"):  # a retry, which is never retried again (retry_job)
        report.update(
            {
                "fallback": job["fallback"],
                "first_error": job.get("first_error"),
                "first_cause": job.get("first_cause"),
            }
        )
    return report


def retry_job(job: dict, report: dict, work: Path) -> dict | None:
    """The job to run again after its car failed, with fallback_for's options, or None: the
    car built, the job was a retry or a --verify-reuse check already, or no fallback helps.
    The failed try's work files are kept as work/<media>.first-try.*."""
    if report.get("ok") or job.get("fallback") or job.get("verify"):
        return None
    retry = fallback_for(report, job["port_options"])
    if not retry:
        return None
    options, why = retry
    media = job["media"]
    for ext in ("json", "log", "fp.json"):  # a crashed worker leaves no report
        if (work / f"{media}.{ext}").exists():
            (work / f"{media}.{ext}").replace(work / f"{media}.first-try.{ext}")
    return {
        **job,
        "port_options": options,
        "fallback": why,
        "first_error": report.get("error"),
        "first_cause": report.get("cause"),
        "seconds_before": report["seconds"],
    }


SECONDS_CACHE = REPO / "out" / "cache" / "car-seconds.json"


def _car_key(job_or_report: dict) -> str:
    return f"{job_or_report['media']}@{job_or_report.get('game', '')}".lower()


def longest_first(jobs: list[dict], cache: Path | None = None) -> list[dict]:
    """The jobs, the cars that took longest in earlier builds first (SECONDS_CACHE; a car
    never built counts as the median), so no long car starts last and runs on alone
    while the other workers idle: 20 cars on 8 workers finish about 13% sooner. The
    order changes nothing in a car's output, and the merge sorts by ID."""
    seconds = read_cache(cache or SECONDS_CACHE)
    known = sorted(v for v in (seconds.get(_car_key(j)) for j in jobs) if isinstance(v, int | float))
    if not known:
        return list(jobs)
    median = known[len(known) // 2]
    return sorted(jobs, key=lambda j: -seconds.get(_car_key(j), median))  # stable: list order among equals


ATTEMPTS_CACHE = REPO / "out" / "cache" / "car-attempts.json"


def remembered_attempts(job: dict, cache: Path | None = None) -> int | None:
    """How many conversion attempts the job's car took in an earlier build (remember_attempts),
    or None: port_car.build_car converts the attempts before that one's full entry alone."""
    try:
        return read_cache(cache or ATTEMPTS_CACHE).get(_car_key(job))
    except AttributeError:  # not a JSON object
        return None


def remember_attempts(reports, cache: Path | None = None) -> None:
    """Keep how many attempts each built car took (port_car.build_car's size retries), for
    the cars whose retries were all of the full entry (a later build's size checks only
    look at it); forget the rest. Of 573 cars, KOE_CCXR_09 retries (4 attempts)."""
    changes = {}
    for r in reports:
        if not (r.get("ok") and r.get("game")):
            continue
        attempts = (r.get("entry") or {}).get("attempts") or []
        retried = len(attempts) > 1 and all(set(a.get("problems", {})) <= {"0"} for a in attempts)
        changes[_car_key(r)] = len(attempts) if retried else None
    path = cache or ATTEMPTS_CACHE
    known = read_cache(path)
    updated = {k: v for k, v in {**known, **changes}.items() if v}
    if updated != known:
        write_cache(path, updated)


def remember_seconds(reports, cache: Path | None = None) -> None:
    """Keep how long each car that built took, for longest_first."""
    new = {_car_key(r): r["seconds"] for r in reports if r.get("ok") and r.get("seconds") and r.get("game")}
    if not new:
        return
    path = cache or SECONDS_CACHE
    seconds = read_cache(path)
    seconds.update(new)
    write_cache(path, seconds)


def read_cache(path: Path) -> dict:
    """A cache of earlier builds (SECONDS_CACHE, ATTEMPTS_CACHE): its JSON, {} when it is
    missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cache(path: Path, data: dict) -> None:
    """Write a cache of earlier builds, sorted by key. Not writing it is only a note: the
    caches steer how a later build runs (its order, a car's first attempt), not what it makes."""
    from dsfforza.build.port_car import write_atomic

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomic(path, json.dumps(dict(sorted(data.items())), indent=0).encode())
    except OSError as error:
        print(f"note: {path.name} not written: {error}")


# --- merge ----------------------------------------------------------------------------------


class MultiStream:
    """Several files read as one address space: the vanilla archive at 0 and each
    car's chunk file after it, so chnk.write_chunk can copy from all of them."""

    def __init__(self, first: Path):
        self.parts: list[tuple[int, int, object]] = []
        self.starts: list[int] = []  # each part's start, ascending (bisect: a merge reads millions of leaves)
        self.size = 0
        self.pos = 0
        self.add(first)

    def add(self, path: Path) -> int:
        """Append a file to the address space, at the next 4 KiB boundary; returns where it
        starts."""
        handle = path.open("rb")
        size = path.stat().st_size
        base = self.size
        self.parts.append((base, base + size, handle))
        self.starts.append(base)
        self.size += -(-size // 0x1000) * 0x1000
        return base

    def seek(self, pos: int, whence: int = 0) -> int:
        self.pos = pos if whence == 0 else self.pos + pos if whence == 1 else self.size + pos
        return self.pos

    def tell(self) -> int:
        return self.pos

    def read(self, n: int = -1) -> bytes:
        """Up to n bytes from the current position, within the file it falls in (b"" between
        files and past the end)."""
        k = bisect.bisect_right(self.starts, self.pos) - 1
        if k < 0:
            return b""
        start, end, handle = self.parts[k]
        if not self.pos < end:
            return b""
        if n < 0:
            n = end - self.pos
        handle.seek(self.pos - start)
        data = handle.read(min(n, end - self.pos))
        self.pos += len(data)
        return data

    def close(self) -> None:
        for _, _, handle in self.parts:
            handle.close()


def merge(
    dsf: Path,
    out: Path,
    results: list[dict],
    work: Path,
    log=print,
    root_limit: int = ENTRY_LIMIT,
    *,
    write: bool = True,
    info: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """Register every built car into a copy of the vanilla archive. Returns (entries
    of the manifest's "cars", failures found here). Cars that define a resource or
    MODL part UID another vehicle defines too are left out (cause "UID clash") and
    the rest are registered again, so one clash doesn't fail the whole batch. `write`
    and `info`: as register_cars (the archive is written only when it fits in a CHNK)."""
    failed: list[dict] = []
    while True:
        cars, found, clashing = register_cars(dsf, out, results, work, log, root_limit, write=write, info=info)
        failed += found
        if not clashing:
            return cars, failed
        log(f"UID clashes in {sorted(clashing)}; merging again without them (rebuild those cars)")
        failed += [
            {
                **r,
                "ok": False,
                "cause": "UID clash",
                "error": f"defines UIDs another vehicle defines too: {', '.join(clashing[r['media']][:4])}",
            }
            for r in results
            if r["media"] in clashing
        ]
        left_out = {f["media"] for f in failed}
        results = [r for r in results if r["media"] not in left_out]
        if not results:
            return [], failed


def register_cars(
    dsf: Path,
    out: Path,
    results: list[dict],
    work: Path,
    log=print,
    root_limit: int = ENTRY_LIMIT,
    *,
    write: bool = True,
    info: dict | None = None,
) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """One merge pass: (manifest "cars", failures, {media: clashing UIDs}). The
    archive is written only when no car clashes, `write` is set and it fits in one CHNK
    (chnk.SIZE_LIMIT: its sizes and offsets are u32); otherwise `out` is removed (an
    earlier build's would not match). `info` (a dict) gets "written" (whether it was),
    "total" (the archive's size as laid out) and "plan" (merged_view's cars: what the
    registered cars were merged from, in order)."""
    from dsfforza.build import clone_vehicle, port_car
    from dsfforza.formats.chnk import read_chunk

    stream = MultiStream(dsf)
    failed = []
    plan = []
    try:
        root = read_chunk(stream, 0)
        entries = root.entries
        cars = []
        for r in sorted(results, key=lambda r: r.get("new_id", r["spool_id"])):
            chunk_path = work / f"{r['media']}.chnk"
            if not chunk_path.exists():
                failed.append({**r, "ok": False, "cause": "merge", "error": f"{chunk_path.name} missing"})
                continue
            base = stream.add(chunk_path)
            try:
                clones = read_chunk(stream, base).entries
            except (ValueError, struct.error) as error:
                failed.append({**r, "ok": False, "cause": "merge", "error": f"{chunk_path.name}: {error}"})
                continue
            if len(clones) != 3 or any(c.tag != "VEHC" for c in clones):
                failed.append({**r, "ok": False, "cause": "merge", "error": "car chunk is not three VEHC entries"})
                continue
            new_id = r.get("new_id", r["spool_id"])
            chunk_id = r["spool_id"]  # the ID the car's chunk holds
            if new_id != r["spool_id"]:
                # The ID lives only in VSVH, VO3D, SPID and UPVH of the full entry (clone_vehicle).
                clone_vehicle.patch_ids(stream, clones[0], r["spool_id"], new_id)
                r = {
                    **r,
                    "spool_id": new_id,
                    "entry": {
                        **r["entry"],
                        "spool_id": new_id,
                        "interior_id": new_id + 1000,
                        "exterior_id": new_id + 2000,
                    },
                }
            if len(entries) + 3 > root_limit:
                failed.append(
                    {
                        **r,
                        "ok": False,
                        "cause": "root entry limit",
                        "error": f"{len(entries)} root entries; the limit is {root_limit} (--root-limit)",
                    }
                )
                continue
            clone_vehicle.register_clones(stream, entries, clones, r["host"], r["spool_id"])
            cars.append(r["entry"])
            plan.append({"chunk": str(chunk_path), "chunk_id": chunk_id, "spool_id": r["spool_id"], "host": r["host"]})
        records = {s: i for s, i, _ in clone_vehicle.sslp_records(stream, entries)}
        for c in cars:
            c["vehc"] = [records[c["spool_id"] + k] for k in clone_vehicle.KIND_OFFSETS]
        clashes = port_car.uid_clashes(stream, entries, {i for c in cars for i in c["vehc"]})
        if clashes:
            return (
                cars,
                failed,
                {
                    c["media"]: sorted({u for i in c["vehc"] for u in clashes.get(i, [])})
                    for c in cars
                    if any(i in clashes for i in c["vehc"])
                },
            )
        write_merged(out, stream, entries, root.version, len(cars), log, write=write, info=info, plan=plan)
    finally:
        stream.close()
    return cars, failed, {}


def write_merged(
    out: Path, stream, entries: list, version: int, count: int, log=print, *, write: bool, info, plan: list
) -> None:
    """register_cars' last step: write the merged archive when `write` is set and it fits in one
    CHNK, else remove `out` and say why (see register_cars)."""
    from dsfforza.formats.chnk import SIZE_LIMIT, layout, write_chunk

    memo: dict = {}
    total = layout(entries, memo)[0]
    if info is not None:
        info.update(written=False, total=total, plan=plan)
    if not write or total > SIZE_LIMIT:
        out.unlink(missing_ok=True)  # an earlier build's archive doesn't hold these cars
        why = (
            "--no-archive"
            if total <= SIZE_LIMIT
            else f"{count} cars make it {total / 2**30:.2f} GiB, and a CHNK's sizes and offsets are u32 (at most 4 GiB)"
        )
        log(
            f"note: {out.name} not written ({why}). The car packages don't need it: lint reads the cars "
            f"merged in memory ({MERGE_PLAN}), the garage pictures render from smaller archives ({SHARDS}/)"
        )
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = out.with_name(out.name + ".tmp")
    try:
        with temp.open("wb") as handle:
            write_chunk(handle, stream, entries, version, memo)
        Path(temp).replace(out)
    finally:
        temp.unlink(missing_ok=True)  # a failed write leaves no 1-2 GB partial copy behind
    if info is not None:
        info["written"] = True
    size = out.stat().st_size
    if size >= 1 << 31:
        # The vanilla file layer clamps seek positions of 2 GiB and up to 0 (spool-limits.md).
        log(
            f"note: {out.name} is {size / 2**30:.2f} GiB; past 2 GiB it needs dsfcars.asi "
            f"[archive] large_files=1 (the default)"
        )


MERGE_PLAN = "dngvehicles.plan.json"  # <build>/: what lint merges in memory when the archive isn't written
SHARDS = "shards"  # <build>/shards/: the garage renders' archives then (removed after the GUI step)


def merged_view(dsf: Path, plan: list[dict]) -> tuple[MultiStream, list]:
    """(stream, root entries) of the merged archive as register_cars lays it out, in
    memory: the vanilla archive and each car of `plan` (register_cars' info["plan"]:
    its chunk file, the ID the chunk holds, its spool ID and host) registered in that
    order, as register_cars registers them. Readers that go through
    clone_vehicle.read_leaf or vehicle_mesh.payload (upvh lint) see what the archive
    would hold; nothing is written, so there is no size limit. The caller closes the
    stream."""
    from dsfforza.build import clone_vehicle
    from dsfforza.formats.chnk import read_chunk

    stream = MultiStream(dsf)
    try:
        entries = read_chunk(stream, 0).entries
        for car in plan:
            base = stream.add(Path(car["chunk"]))
            clones = read_chunk(stream, base).entries
            if car["chunk_id"] != car["spool_id"]:
                clone_vehicle.patch_ids(stream, clones[0], car["chunk_id"], car["spool_id"])
            clone_vehicle.register_clones(stream, entries, clones, car["host"], car["spool_id"])
    except BaseException:
        stream.close()
        raise
    return stream, entries


def write_plan(path: Path, dsf: Path, plan: list[dict]) -> Path:
    """The merge plan `upvh lint-many --plan` reads (merged_view's arguments)."""
    path.write_text(json.dumps({"vanilla": str(dsf), "cars": plan}, indent=1) + "\n", encoding="utf-8")
    return path


def read_plan(path: Path) -> tuple[Path, list[dict]]:
    """The vanilla archive and the cars of a merge plan file (write_plan)."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return Path(data["vanilla"]), data["cars"]


class Shards:
    """Archives of the vanilla one plus some of the cars, each within one CHNK
    (chnk.SIZE_LIMIT), for the garage renders when the merged archive isn't written:
    called with the cars gui_assets.build renders, it writes archives holding them into
    `folder` and returns {spool ID: its archive}. A render reads the car's own three
    VEHC entries and the SSIC's shared materials and textures (car_render._load_scene);
    register_clones changes only SSIC's SSLP and VSCT, so a car draws the same from a
    shard as from the merged archive. close() removes the folder."""

    def __init__(self, dsf: Path, plan: list[dict], folder: Path, log=print, limit: int | None = None):
        from dsfforza.formats.chnk import SIZE_LIMIT

        self.dsf, self.plan, self.folder, self.log = Path(dsf), plan, Path(folder), log
        self.limit = SIZE_LIMIT if limit is None else limit
        self.paths: dict[int, Path] = {}
        self.written = 0

    def __call__(self, cars: list[dict]) -> dict[int, Path]:
        wanted = {c["spool_id"] for c in cars}
        todo = [p for p in self.plan if p["spool_id"] in wanted and p["spool_id"] not in self.paths]
        # Runs of cars (in merge order) whose chunks fit beside the vanilla archive; _write
        # checks each as laid out.
        room = self.limit - self.dsf.stat().st_size
        group, size = [], 0
        for car in todo:
            n = Path(car["chunk"]).stat().st_size
            if group and size + n > room:
                self._write(group)
                group, size = [], 0
            group.append(car)
            size += n
        if group:
            self._write(group)
        return {sid: self.paths[sid] for sid in wanted if sid in self.paths}

    def _write(self, group: list[dict]) -> None:
        from dsfforza.formats.chnk import layout, read_chunk, write_chunk

        stream, entries = merged_view(self.dsf, group)
        try:
            memo: dict = {}
            total = layout(entries, memo)[0]
            if total <= self.limit:
                self.folder.mkdir(parents=True, exist_ok=True)
                path = self.folder / f"dngvehicles-{self.written + 1}.sp"
                with path.open("wb") as handle:
                    write_chunk(handle, stream, entries, read_chunk(stream, 0, depth=0).version, memo)
                self.written += 1
        finally:
            stream.close()
        if total > self.limit:  # the alignment between entries: split the run
            if len(group) == 1:
                raise ValueError(f"{group[0]['chunk']} and the vanilla archive pass {self.limit} bytes")
            half = len(group) // 2
            self._write(group[:half])
            self._write(group[half:])
            return
        self.log(f"garage renders: {path.name}, {len(group)} cars, {total / 2**30:.2f} GiB")
        for car in group:
            self.paths[car["spool_id"]] = path

    def close(self) -> None:
        import shutil

        shutil.rmtree(self.folder, ignore_errors=True)


def plain_glass(car: dict) -> bool:
    """True when the car's converted cockpit has interior glass but no window that
    took crack lines and shards (glass.InteriorGlass leaves lamp covers and small panes
    as surface only: open race cars and roadsters; and windows the host interior has
    none for), as its cockpit report says. Reports from before #459 say "crack_m"."""
    interior = (car.get("cockpit") or {}).get("interior")
    windows = interior.get("glass_windows") if isinstance(interior, dict) else None
    return bool(windows) and not any("host_window" in w or "crack_m" in w for w in windows.values())


LINT_GROUP = 64  # cars per lint process at least: each reads the whole archive's tables first (~20 s)


def lint_all(
    out: Path,
    dsf: Path,
    cars: list[dict],
    parallel: int,
    log=print,
    *,
    facts: bool = False,
    defined: set[int] | frozenset = frozenset(),
    records: list | None = None,
    record_dir: Path | None = None,
) -> dict[str, dict]:
    """upvh lint for every car: up to `parallel` processes of `upvh lint-many`, each over
    a run of the cars (LINT_GROUP or more), so the archive and its cross-vehicle tables
    are read once per process instead of once per car (at hundreds of cars a single lint
    reads the whole archive for seconds). c["lint"] is "ok" or what `upvh lint --id`
    prints for the car. With `facts`: returns {media: upvh.lint_vehicle's facts} (with
    the defined UIDs of the `defined` spool IDs); with `records` (a list) and
    `record_dir`: each process records what it read and ran (lint-many --record) and
    the records are appended (src/dsfforza/build/pack_reuse.py). `out`: the merged archive,
    or its merge plan (MERGE_PLAN, a .json: lint-many --plan merges the cars in memory)."""

    from dsfforza.build import jobpool

    flags = gentle.creation_flags(BELOW_NORMAL if os.name == "nt" else 0)
    processes = max(1, min(parallel, -(-len(cars) // LINT_GROUP)))
    # Slots of the machine's job pool (jobpool.py): one waited for, the rest as free now.
    slots = jobpool.lease("lint", processes, LINT_RAM, log=log, child=True)
    processes = len(slots)
    size = -(-len(cars) // processes) if cars else 1
    groups = [cars[k : k + size] for k in range(0, len(cars), size)]
    for extra in slots[max(1, len(groups)) :]:  # fewer groups than slots: no process for these
        extra.release()
    procs = []
    try:
        for n, group in enumerate(groups):
            source = ["--plan", str(out)] if Path(out).suffix == ".json" else [str(out)]
            command = [sys.executable, "-m", "dsfforza.formats.upvh", "lint-many", *source, "--vanilla", str(dsf)]
            for c in group:
                command += ["--car", f"{c['spool_id']}:{c['host']}"]
                if plain_glass(c):
                    command += ["--plain-glass", str(c["spool_id"])]
                if facts and c["spool_id"] in defined:
                    command += ["--defined", str(c["spool_id"])]
            if facts:
                command += ["--facts"]
            record = None
            if records is not None and record_dir is not None:
                record_dir.mkdir(parents=True, exist_ok=True)
                record = record_dir / f"lint-record-{n}.json"
                record.unlink(missing_ok=True)
                command += ["--record", str(record)]
            procs.append(
                (
                    group,
                    record,
                    subprocess.Popen(
                        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, creationflags=flags
                    ),
                )
            )
            jobpool.adopt(slots[n], procs[-1][2])  # it ends with this process, its memory counts against its slot
        return _lint_results(procs, records, log)
    finally:
        for slot in slots:
            slot.release()


def _lint_results(procs: list, records: list | None, log) -> dict[str, dict]:
    """lint_all's results: c["lint"] for each car of each process, and the facts."""
    found: dict[str, dict] = {}
    for group, record, proc in procs:
        text, _ = proc.communicate()
        rows = {}
        for line in text.splitlines():
            if line.startswith("{"):
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                rows[row.get("id")] = row
        for c in group:
            row = rows.get(c["spool_id"])
            if row is None:  # the process died before this car
                c["lint"] = f"lint did not run (upvh lint-many exit code {proc.returncode}): {text.strip()[-1500:]}"
            else:
                c["lint"] = "ok" if row["ok"] else row["text"].strip()
                if "facts" in row:
                    found[c["media"]] = row["facts"]
            log(f"  lint {c['spool_id']} {c['media']}: {'ok' if c['lint'] == 'ok' else 'FAILED'}")
        if record is not None:
            try:
                records.append(json.loads(record.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                records.append({"complete": False})  # the pack record won't be reused
            record.unlink(missing_ok=True)
    return found


# --- commands ---------------------------------------------------------------------------------


def common(args) -> tuple[Path, list[Path], dict, dict[str, Path]]:
    """What every command needs first: DSF_GAME_DIR from --dsf, the FM4 DLC packs (or
    --no-fm4-dlc), Below Normal priority, the disc images identified by their content,
    game_data prepare, the roster, the gamedbs and the string tables. Returns (dsf, isos,
    cars, dbs)."""
    set_game_env(args.dsf)
    if getattr(args, "no_fm4_dlc", False):
        from dsfforza.build import fm4_dlc

        fm4_dlc.disable()  # this process and its workers
    lower_priority()
    isos = [p.resolve() for p in (args.iso or forza_roster.default_images())]
    if not isos:
        raise SystemExit("no Forza disc images (assets/*.iso or --iso)")
    # What each image is, from its default.xex (any file names); a missing game's cars are skipped.
    discs = forza_roster.identify_all(isos)
    found, notes = forza_roster.disc_summary(discs)
    if not found:
        raise SystemExit("no Forza disc among the images: " + "; ".join(f"{d.path.name}: {d.error}" for d in discs))
    print(f"disc images: {', '.join(found)}")
    for note in notes:
        print(f"  {note}")
    from dsfforza.build import fm4_dlc

    packs = fm4_dlc.packages()
    if packs:
        print(f"FM4 DLC packs: {len(packs)} ({', '.join(p.name for p in packs)})")
    elif not fm4_dlc.enabled():
        print("FM4 DLC packs: left out (--no-fm4-dlc)")
    from dsfforza.game import game_data

    game_data.prepare(game_from_dsf(args.dsf), isos, log=lambda t: print(f"prepare: {t}"))
    cars = load_roster(isos)
    dbs = gamedbs(isos, args.cache)
    string_tables(isos)
    return args.dsf, isos, cars, dbs


def string_tables(isos: list[Path]) -> None:
    """Forza's string tables (car, maker and family names) into out/cache/<game>/strings
    from the given disc images, found by title: gui_assets.car_texts and car_groups read
    them there, whatever the images are called."""
    from dsfforza.build.forza_roster import game_discs
    from dsfforza.formats import forza_strings

    for game, iso in game_discs(isos).items():
        if game in ("FH1", "FM4"):
            try:
                forza_strings.Strings(game.lower(), iso=iso)
            except Exception as error:  # noqa: BLE001 - printed; names then come from the media names
                print(f"warning: {game} string tables from {iso.name}: {type(error).__name__}: {error}")


def vanilla_ids(dsf: Path) -> tuple[int, set[int]]:
    """(how many root entries the vanilla archive has, the spool IDs its vehicles take)."""
    from dsfforza.build import clone_vehicle
    from dsfforza.formats.chnk import read_chunk

    with dsf.open("rb") as stream:
        entries = read_chunk(stream, 0).entries
        return len(entries), clone_vehicle.taken_ids(stream, entries)


def print_plan(picks: list[Pick]) -> None:
    """The plan as a table: ID, car, game, year, body, drive, power, host and counterpart, notes."""
    print(f"{'ID':>4}  {'car':30} {'game':4} {'year':4}  {'body':11} {'drive':5} {'kW':>5}  host / counterpart")
    for p in picks:
        cp = f"   vanilla {p.counterpart} {p.counterpart_model}" if p.counterpart else ""
        guess = "?" if p.host_guess else ""
        host = f"{p.host} {p.host_model[:26]} ({p.host_score:.0f}{guess})" if p.host else "-"
        print(
            f"{p.spool_id or '':>4}  {p.media:30} {p.game:4} {p.year or '':4}  {p.body:11} {p.drive or '':5} "
            f"{p.power_kw or 0:5.0f}  {host}{cp}"
            + (f"  !! {'; '.join(p.notes)}" if p.notes else "")
            + (f"  ({p.notes_soft})" if p.notes_soft else "")
        )
    if any(p.host_guess and p.host for p in picks):
        print("(score?): car not extracted yet, its door count from the body class; the build may pick another host")


def cmd_plan(args) -> None:
    """port_batch plan: the selection with its hosts and IDs as a table (and with --tsv as a
    tab-separated file); nothing is built."""
    dsf, isos, cars, dbs = common(args)
    picks = select(args, cars, dsf, dbs)
    _, taken = vanilla_ids(dsf)
    assign_ids([p for p in picks if not p.notes], taken, args.id_policy, args.first_id, isos)
    print_plan(picks)
    print(f"{len(picks)} cars")
    if args.tsv:
        cols = [
            "spool_id",
            "media",
            "game",
            "year",
            "make",
            "body",
            "drive",
            "power_kw",
            "host",
            "host_model",
            "host_score",
            "counterpart",
            "counterpart_model",
            "notes",
        ]
        rows = ["\t".join(cols)] + [
            "\t".join(str(getattr(p, c) if c != "notes" else "; ".join(p.notes)).replace("None", "") for c in cols)
            for p in picks
        ]
        args.tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
        print(f"wrote {args.tsv}")


def base_rev(args) -> str:
    """--base REV as a commit, default the merge base of HEAD and origin/master."""
    from dsfforza.build import carstore

    rev = getattr(args, "base", None)
    if not rev:
        return carstore.merge_base("origin/master")
    out = subprocess.run(
        ["git", "rev-parse", "--verify", f"{rev}^{{commit}}"], cwd=REPO, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        raise SystemExit(f"--base {rev}: not a commit here")
    return out.stdout.strip()


def affected_cars(args, picks: list[Pick], log=print) -> dict[str, dict]:
    """carstore.affected for the picks, from the car store, against --base; printed."""
    from dsfforza.build import carstore

    store = carstore.Store.default()
    if store is None:
        raise SystemExit("--affected needs the car store (carstore.py; DSFFORZA_STORE is off)")
    base = base_rev(args)
    found = carstore.affected(store, [(p.media, p.spool_id) for p in picks], base)
    by = {k: [m for m, f in found.items() if f["status"] == k] for k in ("affected", "same", "unknown")}
    log(
        f"since {base[:10]}: {len(by['affected'])} cars affected, {len(by['same'])} the same, "
        f"{len(by['unknown'])} unknown (no car in the store made by {base[:10]}'s code)"
    )
    reasons: dict[str, list[str]] = {}
    for media in by["affected"]:
        for why in found[media]["why"]:
            reasons.setdefault(why, []).append(media)
    for why, cars in sorted(reasons.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        log(f"  {why}: {len(cars)} ({name_list(cars, 6)})")
    if by["unknown"]:
        log(f"  unknown: {name_list(by['unknown'], 12)}")
    return found


def name_list(names: list[str], limit: int) -> str:
    """The first `limit` names, comma-separated, and ", ..." when there are more."""
    return ", ".join(names[:limit]) + (", ..." if len(names) > limit else "")


def cmd_affected(args) -> None:
    """Which of the selected cars a change since --base affects, from the car store's
    fingerprints (carstore.affected), without building anything."""
    dsf, isos, cars, dbs = common(args)
    picks = [p for p in select(args, cars, dsf, dbs) if not p.notes]
    _, taken = vanilla_ids(dsf)
    assign_ids(picks, taken, args.id_policy, args.first_id, isos)
    found = affected_cars(args, picks)
    if args.write_list:
        todo = [p for p in picks if found[p.media]["status"] != "same"]
        args.write_list.write_text(
            "".join(f"{p.media}@{p.game.lower()}\n" for p in todo), encoding="utf-8", newline="\n"
        )
        print(f"wrote {args.write_list}: {len(todo)} cars (affected and unknown)")
    if args.json:
        args.json.write_text(json.dumps(found, indent=1), encoding="utf-8")


WORK_FILES = ("chnk", "json", "job.json", "log", "fp.json", "first-try.json", "first-try.log", "first-try.fp.json")
STORE = "car store"  # Reuse.taken_from for a car taken from the car store


class Reuse:
    """build --reuse DIR: a car whose earlier output in DIR/work was made from the same
    job, the same input files and the same code (src/dsfforza/build/fingerprint.py) is
    copied instead of converted. --rebuild-where builds the cars an expression over their
    earlier report picks again anyway; --trust-code leaves the code out of the comparison
    (the expression then names the cars a change affects). --verify-reuse N converts N
    of the reused cars again and stops the build if any comes out different.
    --reuse may be given more than once: each car is taken from the first build that
    has a matching copy of it (src/dsfforza/build/install.py passes the last finished
    build and an interrupted one). After them, the car store every worktree shares
    (`store`, src/dsfforza/build/carstore.py), checked the same way."""

    def __init__(self, args, work: Path, store=None):
        from dsfforza.build import fingerprint

        given = args.reuse if isinstance(args.reuse, list | tuple) else [args.reuse] if args.reuse else []
        self.sources = [Path(s) for s in given]
        self.store = store
        for source in self.sources:
            if not (source / "work").is_dir():
                raise SystemExit(f"--reuse {source}: no work/ folder there")
            if (source / "work").resolve() == work.resolve():
                raise SystemExit("--reuse names this build's own --out (use --resume for that)")
        self.source = self.sources[0] if self.sources else None
        self.work = work
        self.where = getattr(args, "rebuild_where", None)
        self.trust_code = getattr(args, "trust_code", False)
        # --verify-reuse: default 2 with --reuse, none for store hits alone (a store hit is
        # an earlier build of the same commit, on this PC; --verify-reuse N asks for N).
        count = getattr(args, "verify_reuse", None)
        self.verify_count = count if count is not None else (2 if self.sources else 0)
        self.seed = getattr(args, "verify_seed", None)
        self.checker = fingerprint.Checker()
        self.taken: dict[str, dict] = {}
        self.taken_from: dict[str, str] = {}  # media -> the build it was taken from
        self.rebuilt: dict[str, str] = {}
        self.verified: dict[str, str] = {}
        self.mismatch: dict = {}  # --verify-mismatch rebuild: what a mismatch replaced (record())

    def _predicate(self, report: dict, job: dict) -> bool:
        names = {
            "r": report,
            "report": report,
            "entry": report.get("entry") or {},
            "media": job["media"],
            "game": job["game"],
            "host": report.get("host"),
        }
        safe = {
            "any": any,
            "all": all,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "set": set,
            "sorted": sorted,
            "min": min,
            "max": max,
            "abs": abs,
            "bool": bool,
        }
        try:
            return bool(eval(self.where, {"__builtins__": safe}, names))
        except Exception as error:
            raise SystemExit(
                f"--rebuild-where {self.where!r} on {job['media']}: {type(error).__name__}: {error}"
            ) from error

    def decide_from(self, job: dict) -> tuple[dict | None, Path | None, str]:
        """(the earlier report, the build it is in, "") for the first --reuse build that
        has a matching copy of the car, else (None, None, why the first build's doesn't)."""
        first_why = None
        for source in self.sources:
            report, why = self._decide_in(source / "work", job)
            if report is not None:
                return report, source, ""
            first_why = first_why or why
        if self.store is not None:
            entry, why = self.store.find(job, self.checker, code=not self.trust_code)
            if entry is not None and self.where:
                report = entry.report() or {}
                if self._predicate(report, job):
                    entry, why = None, "--rebuild-where"
            if entry is not None:
                return None, entry, ""
            first_why = first_why or why
        return None, None, first_why or "not built there"

    def _decide_in(self, old: Path, job: dict) -> tuple[dict | None, str]:
        from dsfforza.build import fingerprint

        media = job["media"]
        report = fingerprint.read(old / f"{media}.json")
        if not report or not report.get("ok") or not (old / f"{media}.chnk").exists():
            return None, "not built there"
        fp = fingerprint.read(old / f"{media}.fp.json")
        if fp is None:
            return None, "no fingerprint"
        current = {**job, "requested_options": job["port_options"]}
        why = self.checker.check(fp, current, code=not self.trust_code)
        first = old / f"{media}.first-try.fp.json"
        if why is None and first.exists():  # a fallback build: the first try must be unchanged too
            moved = self.checker.moved(fp, current)
            why = self.checker.check(fingerprint.read(first) or {}, None, code=not self.trust_code, moved=moved)
            why = why and f"first try: {why}"
        if why is None and self.where and self._predicate(report, job):
            why = "--rebuild-where"
        return (report, "") if why is None else (None, why)

    def take(self, job: dict) -> dict | None:
        """Copy the car's earlier output into this build's work/ (and sound/) if it can be
        reused; returns its report."""
        import shutil

        report, source, why = self.decide_from(job)
        media = job["media"]
        if source is not None and not isinstance(source, Path):  # a car store entry
            report = self.store.take(source, self.work, Path(job["sound_dir"]))
            if report is None:
                self.rebuilt[media] = "store: the entry went meanwhile"
                return None
            self.taken[media] = report
            self.taken_from[media] = STORE
            return {**report, "reused_from": STORE}
        if report is None:
            self.rebuilt[media] = why
            return None
        for name in WORK_FILES:
            src = source / "work" / f"{media}.{name}"
            if src.exists():
                _link_or_copy(src, self.work / f"{media}.{name}")
        sound_src = source / "sound" / media
        if sound_src.is_dir():
            dst = Path(job["sound_dir"]) / media
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(sound_src, dst, copy_function=_link_or_copy)
        self.taken[media] = report
        self.taken_from[media] = str(source)
        return {**report, "reused_from": str(source)}

    def summary(self) -> None:
        """Print how many cars are reused and from where, and why the others are built again
        (by reason)."""
        names = ", ".join([str(s) for s in self.sources] + ([f"the car store {self.store.root}"] if self.store else []))
        stored = sum(1 for m in self.taken if self.taken_from.get(m) == STORE)
        print(
            f"reuse from {names}: {len(self.taken)} cars reused"
            + (f" ({stored} from the store)" if self.store else "")
            + f", {len(self.rebuilt)} to build"
        )
        reasons: dict[str, list[str]] = {}
        for media, why in self.rebuilt.items():
            reasons.setdefault(why, []).append(media)
        for why, cars in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            print(f"  {why}: {len(cars)} ({name_list(cars, 6)})")

    def verify(self, jobs: list[dict], parallel: int, progress=None, on_mismatch: str = "stop") -> list[str]:
        """Convert a random few of the reused cars again into work/verify/ and require the
        same bytes. When any differs (or fails), on_mismatch "stop" raises SystemExit
        (before the merge: port_batch build, for developers); "rebuild" (dsff install, for
        players) keeps going: a car that came out different takes its fresh
        copy, and the cars to convert again are returned: the other reused cars of the
        builds the bad ones came from (a mismatch can't be pinned to its car: a missed
        input may be shared) and the ones whose fresh conversion failed."""
        import random
        import shutil

        if not self.verify_count or not self.taken:
            return []
        seed = self.seed if self.seed is not None else random.randrange(1 << 30)
        sample = random.Random(seed).sample(sorted(self.taken), min(self.verify_count, len(self.taken)))
        folder = self.work / "verify"
        folder.mkdir(exist_ok=True)
        by_media = {j["media"]: j for j in jobs}
        checks = []
        for media in sample:
            old = json.loads((self.work / f"{media}.job.json").read_text(encoding="utf-8"))
            for name in WORK_FILES:
                (folder / f"{media}.{name}").unlink(missing_ok=True)
            checks.append(
                {
                    **by_media[media],
                    "port_options": old.get("port_options", by_media[media]["port_options"]),  # after a fallback
                    "fallback": old.get("fallback"),
                    "verify": True,
                    "chunk": str(folder / f"{media}.chnk"),
                    "report": str(folder / f"{media}.json"),
                    "sound_dir": str(folder / "sound"),
                }
            )
        print(f"--verify-reuse: building {len(sample)} reused cars again (seed {seed}): {', '.join(sample)}")
        emit(progress, "verify", cars=sample)
        results = run_workers(checks, folder, parallel, progress=progress)
        bad = []
        for media in sample:
            new = folder / f"{media}.chnk"
            if not results.get(media, {}).get("ok") or not new.exists():
                self.verified[media] = "failed: " + str(results.get(media, {}).get("error", "no output"))[:200]
            elif new.read_bytes() != (self.work / f"{media}.chnk").read_bytes():
                self.verified[media] = "DIFFERENT"
            else:
                self.verified[media] = "identical"
            if self.verified[media] != "identical":
                bad.append(f"{media} ({self.verified[media]})")
        if bad and on_mismatch != "rebuild":
            raise SystemExit(
                f"--verify-reuse (seed {seed}): {len(bad)} of {len(sample)} reused cars came out different when built "
                f"again: {'; '.join(bad)}. Either their fingerprints missed a change or the conversion isn't "
                f"repeatable (research/batch.md, 'Selective rebuild'); build without --reuse, or with "
                f"--rebuild-where covering them."
            )
        if not bad:
            print(f"--verify-reuse: {len(sample)} of {len(sample)} identical")
            return []
        differ = [m for m in sample if self.verified[m] == "DIFFERENT"]
        failed = [m for m in sample if self.verified[m].startswith("failed")]
        for media in differ:  # the fresh copy replaces the reused one
            for name in WORK_FILES:
                (self.work / f"{media}.{name}").unlink(missing_ok=True)
                if (folder / f"{media}.{name}").exists():
                    (folder / f"{media}.{name}").replace(self.work / f"{media}.{name}")
            sound = Path(by_media[media]["sound_dir"]) / media
            shutil.rmtree(sound, ignore_errors=True)
            if (folder / "sound" / media).is_dir():
                shutil.move(str(folder / "sound" / media), str(sound))
            check = next(c for c in checks if c["media"] == media)
            job = {**by_media[media], "port_options": check["port_options"], "fallback": check["fallback"]}
            (self.work / f"{media}.job.json").write_text(json.dumps(job, indent=1), encoding="utf-8")
            self.taken.pop(media, None)
        sources = {self.taken_from.get(m) for m in differ + failed}
        redo = sorted(
            m
            for m in self.taken
            if m not in self.verified or self.verified[m] != "identical"
            if self.taken_from.get(m) in sources
        )
        for media in redo:
            self.taken.pop(media, None)
        self.mismatch = {
            "seed": seed,
            "different": differ,
            "failed": failed,
            "fresh_copies": differ,
            "converted_again": redo,
            "sources": sorted(s for s in sources if s),
        }
        print(
            f"--verify-reuse (seed {seed}): {'; '.join(bad)}: came out different when built again. "
            f"Using the fresh copies, and converting the {len(redo)} other cars reused from "
            f"{', '.join(self.mismatch['sources']) or 'there'} again (--verify-mismatch rebuild)."
        )
        emit(progress, "verify_mismatch", cars=bad, redo=redo)
        return redo

    def record(self) -> dict:
        """For batch-report.json (timings_s.reuse)."""
        return {
            "from": str(self.source) if self.sources else STORE,
            **(
                {
                    "store": str(self.store.root),
                    "from_store": sorted(m for m, s in self.taken_from.items() if s == STORE),
                }
                if self.store
                else {}
            ),
            **({"sources": [str(s) for s in self.sources]} if len(self.sources) > 1 else {}),
            "reused": sorted(self.taken),
            "rebuilt": self.rebuilt,
            "verified": self.verified,
            **({"verify_mismatch": self.mismatch} if self.mismatch else {}),
            "trust_code": self.trust_code,
            "rebuild_where": self.where,
        }


def _link_or_copy(src, dst) -> None:
    """A hard link (the work files are never written in place), else a copy."""
    import shutil

    dst = Path(dst)
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def cmd_build(args) -> None:
    """port_batch build: plan the selection, take what earlier builds and the car store
    have, convert the rest, then merge, lint and pack every car (finish)."""
    started = time.time()
    timings = {}
    dsf, isos, cars, dbs = common(args)
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "dngvehicles.sp"
    if archive.resolve() == dsf.resolve():
        raise SystemExit("refusing to overwrite the vanilla archive")
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    port_options = shlex.split(args.port if args.port is not None else DEFAULT_PORT_OPTIONS, posix=True)
    port_args = port_car_args(["--dsf", str(dsf), "--out", str(archive), "--car", "X"] + port_options)
    progress = getattr(args, "progress", None)

    picks, skipped, root_limit = plan_build(args, dsf, isos, cars, dbs)
    warm_caches(dsf, port_args)
    timings["plan"] = round(time.time() - started, 1)

    jobs = build_jobs(picks, dsf, isos, args.cache, port_options, out_dir)
    store, reuse, before = open_reuse(args, work)
    t = time.time()
    reused = take_earlier(jobs, work, resume=args.resume, reuse=reuse)
    if reuse is not None:
        timings["reuse_check"] = round(time.time() - t, 1)
    for job in jobs:
        job["requested_options"] = list(job["port_options"])
    t = time.time()
    args.jobs = gentle.jobs(args.jobs)
    converted = convert_cars(jobs, reused, work, args.jobs, reuse, before=before, progress=progress)
    results = {**reused, **converted}
    timings["convert"] = round(time.time() - t, 1)
    if reuse is not None:
        verify_reused(args, reuse, jobs, work, results, timings)
    if store is not None:
        t = time.time()
        store_cars(store, reuse, jobs, work, results)
        timings["store"] = round(time.time() - t, 1)
    ok, failed = split_results(picks, skipped, results)
    pack = open_pack_reuse(args, dsf, out_dir, root_limit, reuse, store)
    try:
        finish(args, dsf, out_dir, work, ok, failed, port_args, port_options, timings, started, results, pack)
    finally:
        if pack is not None and pack.recorder is not None:
            pack.recorder.stop()


def plan_build(args, dsf: Path, isos: list[Path], cars: dict, dbs: dict[str, Path]) -> tuple[list, list, int]:
    """build's plan: (the selected cars with their IDs, the ones left out with their notes,
    the root limit). IDs are given over the whole selection; with --affected only the cars
    a change since --base affects stay. The plan is printed and sent as the plan event."""
    picks = select(args, cars, dsf, dbs)
    skipped = [p for p in picks if p.notes]
    picks = [p for p in picks if not p.notes]
    vanilla_entries, taken = vanilla_ids(dsf)
    assign_ids(picks, taken, args.id_policy, args.first_id, isos)
    root_limit = root_limit_of(args)
    room = (root_limit - vanilla_entries) // 3  # 453 root entries in the vanilla archive
    if len(picks) > room:
        raise SystemExit(
            f"{len(picks)} cars: the archive root holds at most {room} new ones (--root-limit {root_limit})"
        )
    if getattr(args, "affected", False):
        # Only the cars a change since --base affects (and those the store can't tell),
        # with the IDs of the whole selection.
        found = affected_cars(args, picks)
        picks = [p for p in picks if found[p.media]["status"] != "same"]
        if not picks:
            raise SystemExit("no car of the selection is affected")
    print_plan(picks + skipped)
    emit(
        getattr(args, "progress", None),
        "plan",
        cars=[p.media for p in picks],
        skipped=[{"media": p.media, "error": "; ".join(p.notes)} for p in skipped],
    )
    return picks, skipped, root_limit


def warm_caches(dsf: Path, port_args) -> None:
    """Fill the caches every worker reads once, so the workers only read them: the host
    survey, the handling fleet (--handling dsf) and the vanilla sound banks' index."""
    from dsfforza.build import host_select

    host_select.survey(dsf)
    if port_args.handling == "dsf" and not port_args.no_handling:
        from dsfforza.build import handling_dsf

        handling_dsf.load_fleet(dsf)
    if not port_args.no_forza_sound:
        from dsfforza.convert import engine_sound

        engine_sound.vanilla_index(dsf)  # templates and reserved Wwise IDs (out/cache/sound/vanilla-banks.json)


def build_jobs(
    picks: list[Pick], dsf: Path, isos: list[Path], cache: Path, port_options: list[str], out_dir: Path
) -> list[dict]:
    """A worker job per car: the car and its ID, the inputs and port_car options
    (fingerprint.job_key reads them), and where its outputs go (out_dir/work, out_dir/sound)."""
    work = out_dir / "work"
    return [
        {
            "media": p.media,
            "game": p.game,
            "year": p.year,
            "games": p.games,
            "source": p.source,
            "spool_id": p.spool_id,
            "dsf": str(dsf),
            "game_dir": str(game_from_dsf(dsf)),
            "cache": str(cache),
            "isos": [str(i) for i in isos],
            "port_options": port_options,
            "sound_dir": str(out_dir / "sound"),
            "chunk": str(work / f"{p.media}.chnk"),
            "report": str(work / f"{p.media}.json"),
        }
        for p in picks
    ]


def open_reuse(args, work: Path) -> tuple[object, Reuse | None, bool]:
    """(the car store every worktree on the PC shares, the Reuse that takes cars from it and
    from the --reuse builds, --before). The store is None with --no-store (or
    DSFFORZA_STORE=off), the Reuse None without a store and --reuse. With --before the
    Reuse checks the store's cars against --base's code, not this checkout's."""
    from dsfforza.build import carstore

    # Cars converted anywhere from the same job, inputs and code are taken, not converted again.
    store = None if getattr(args, "no_store", False) else carstore.Store.default()
    before = getattr(args, "before", False)
    if before and (store is None or getattr(args, "reuse", None)):
        raise SystemExit("--before takes the cars from the car store only (no --reuse, no --no-store)")
    reuse = Reuse(args, work, store) if getattr(args, "reuse", None) or store is not None else None
    if before:
        # The cars as --base's code made them: the store's entries checked against that
        # revision's code units; none is converted (this checkout's code isn't that).
        from dsfforza.build import fingerprint

        base = base_rev(args)
        reuse.checker = fingerprint.Checker(units=fingerprint.GitUnits(base))
        print(f"--before: every car from the car store as {base[:10]}'s code made it")
    return store, reuse, before


def resumed_report(job: dict, work: Path) -> dict | None:
    """--resume: the report of the car when an earlier run in this folder built it with the
    same ID, game, vanilla archive and options, else None."""
    old_job, old_report = work / f"{job['media']}.job.json", Path(job["report"])
    if not (old_job.exists() and old_report.exists() and Path(job["chunk"]).exists()):
        return None
    try:
        earlier = json.loads(old_job.read_text(encoding="utf-8"))
        report = json.loads(old_report.read_text(encoding="utf-8"))
    except ValueError:  # cut short by an earlier crash: build the car again
        return None
    same = (
        all(earlier.get(k) == job[k] for k in ("spool_id", "game", "dsf"))
        and earlier.get("requested_options", earlier.get("port_options")) == job["port_options"]
    )
    return report if same and report.get("ok") else None


def clear_work(work: Path, media: str) -> None:
    """Remove everything an earlier run left in work/ for a car (its work files and
    half-written temporaries), so nothing stale is read as this run's."""
    for name in WORK_FILES + ("tmp", "json.tmp", "fp.json.tmp"):
        (work / f"{media}.{name}").unlink(missing_ok=True)


def take_earlier(jobs: list[dict], work: Path, *, resume: bool, reuse: Reuse | None) -> dict[str, dict]:
    """{media: report} of the cars an earlier build made: with --resume this folder's own
    (resumed_report), else what `reuse` copies from the --reuse builds and the car store.
    The other cars' leftovers in work/ are removed."""
    reused = {}
    for job in jobs:
        report = resumed_report(job, work) if resume else None
        if report is not None:
            reused[job["media"]] = report
            continue
        clear_work(work, job["media"])
        if reuse is not None:
            report = reuse.take(job)
            if report is not None:
                reused[job["media"]] = report
    return reused


def convert_message(todo: int, jobs: int, reused: dict, reuse: Reuse | None) -> str:
    """The line build prints before converting: how many cars, how many at a time and at
    which priority, and where the reused ones came from."""
    priority = "Idle priority: out/gentle" if gentle.enabled() else "Below Normal priority"
    text = f"building {todo} cars, {jobs} at a time ({priority})"
    if not reused:
        return text
    if reuse is not None and reuse.taken:
        sources = [*map(str, reuse.sources), *([STORE] if reuse.store else [])]
        return text + f"; {len(reused)} reused from {', '.join(sources)}"
    return text + f"; {len(reused)} reused from the last run"


def convert_cars(
    jobs: list[dict], reused: dict, work: Path, parallel: int, reuse: Reuse | None, *, before: bool, progress
) -> dict[str, dict]:
    """Convert every car not taken from an earlier build (run_workers, the longest first),
    and remember their times and attempts for later builds. With --before none is
    converted: each is a failure, "not in the car store". Returns {media: report}."""
    todo = [j for j in jobs if j["media"] not in reused]
    missing = {}
    if before:
        missing = {j["media"]: reuse.rebuilt.get(j["media"], "not in the store") for j in todo}
        todo = []
    print(convert_message(len(todo), parallel, reused, reuse))
    if reuse is not None:
        reuse.summary()
    emit(
        progress,
        "convert",
        todo=[j["media"] for j in todo],
        reused=sorted(reused),
        rebuilt=dict(reuse.rebuilt) if reuse is not None else {},
        jobs=parallel,
        gentle=gentle.enabled(),
    )
    for job in todo:
        job["probe_attempts"] = remembered_attempts(job)
    converted = run_workers(longest_first(todo), work, parallel, progress=progress)
    for media, why in missing.items():  # --before: no car in the store made by --base's code
        converted[media] = {"media": media, "ok": False, "cause": "not in the car store", "error": why}
    remember_seconds(converted.values())
    remember_attempts(converted.values())
    return converted


def verify_reused(args, reuse: Reuse, jobs: list[dict], work: Path, results: dict, timings: dict) -> None:
    """--verify-reuse: a few reused cars converted again (Reuse.verify). One that differs
    stops the build (--verify-mismatch stop), or (rebuild, dsff install) its fresh copy is
    kept and its build's other cars are converted again; `results` gets their reports.
    The reuse record goes to args.reuse_record for the report."""
    progress = getattr(args, "progress", None)
    t = time.time()
    redo = reuse.verify(jobs, args.jobs, progress=progress, on_mismatch=getattr(args, "verify_mismatch", "stop"))
    timings["verify_reuse"] = round(time.time() - t, 1)
    if redo:
        import shutil

        t = time.time()
        by_media = {j["media"]: j for j in jobs}
        for media in redo:
            clear_work(work, media)
            shutil.rmtree(Path(by_media[media]["sound_dir"]) / media, ignore_errors=True)
        results.update(run_workers([by_media[m] for m in redo], work, args.jobs, progress=progress))
        timings["convert_again"] = round(time.time() - t, 1)
    for media in reuse.mismatch.get("fresh_copies", []):
        fresh = Path(work / f"{media}.json")
        with contextlib.suppress(OSError, ValueError):
            results[media] = json.loads(fresh.read_text(encoding="utf-8"))
    args.reuse_record = reuse.record()


def store_cars(store, reuse: Reuse | None, jobs: list[dict], work: Path, results: dict) -> None:
    """Put the cars this build converted, and those it took from elsewhere (--reuse,
    --resume), into the car store for every other build (the same car is kept once); not
    the ones it took from the store. Then let the store evict what it doesn't keep."""
    for job in jobs:
        if results.get(job["media"], {}).get("ok") and (reuse is None or reuse.taken_from.get(job["media"]) != STORE):
            store.put(work, job["media"], Path(job["sound_dir"]))
    store.evict()


def split_results(picks: list[Pick], skipped: list[Pick], results: dict) -> tuple[list[dict], list[dict]]:
    """(the built cars' reports in plan order, the failures: the cars that failed, then the
    ones the plan left out, cause "not selected")."""
    ok = [results[p.media] for p in picks if results.get(p.media, {}).get("ok")]
    failed = [results[p.media] for p in picks if not results.get(p.media, {}).get("ok")]
    failed += [{"media": p.media, "ok": False, "cause": "not selected", "error": "; ".join(p.notes)} for p in skipped]
    return ok, failed


def open_pack_reuse(args, dsf: Path, out_dir: Path, root_limit: int, reuse: Reuse | None, store):
    """The pack_reuse.PackReuse that takes what didn't change from this folder's own earlier
    pack (--resume, a rerun) and the --reuse builds' packs, or None with --no-pack-reuse."""
    if getattr(args, "no_pack_reuse", False):
        return None
    from dsfforza.build import pack_reuse

    settings = pack_reuse.settings_of(args, dsf, root_limit, gui_isos(args))
    return pack_reuse.PackReuse(
        out_dir, reuse.sources if reuse is not None else [], settings, game_dir=game_from_dsf(dsf), store=store
    )


def finish(
    args,
    dsf: Path,
    out_dir: Path,
    work: Path,
    ok: list[dict],
    failed: list[dict],
    port_args,
    port_options: list[str],
    timings: dict,
    started: float,
    results: dict,
    pack=None,
) -> None:
    """Merge, lint, manifest, Lua, GUI assets and the report. `pack`: a
    pack_reuse.PackReuse, to take from earlier builds what didn't change (the whole
    pack, lint results, garage pictures) and record this pack for later ones."""
    archive = out_dir / "dngvehicles.sp"
    progress = getattr(args, "progress", None)
    if not ok:
        write_report(out_dir, archive, [], failed, timings, started, args)
        raise SystemExit("no car was built")
    verify = copy.deepcopy((ok, failed)) if pack is not None and getattr(args, "verify_pack", False) else None
    reused = take_whole_pack(pack, ok, work, timings, progress)
    merged: dict = {}
    if reused is not None:
        cars_out, merge_failed = reused
        failed += merge_failed
        emit_failures(progress, merge_failed)
    else:
        cars_out, merge_failed = merge_built(args, dsf, archive, ok, work, merged, timings, progress)
        failed += merge_failed
        emit_failures(progress, merge_failed)
        if not cars_out:
            if pack is not None:
                pack.forget()
            write_report(out_dir, archive, [], failed, timings, started, args, results)
            raise SystemExit("no car could be merged")
        lint_merged(args, dsf, out_dir, archive, cars_out, merged, pack, timings)
    report_lint(cars_out, progress)
    manifest_path = write_manifest(args, dsf, out_dir, archive, cars_out, failed, port_args, port_options)
    if args.gui and reused is None:
        build_gui(args, dsf, out_dir, archive, manifest_path, cars_out, merged, pack, timings)
    if pack is not None:
        t = time.time()
        pack.save(
            merge_failed,
            pack_outputs(out_dir, cars_out, gui=args.gui) + pack.image_outputs(),
            timings.get("gui_error"),
        )
        timings["pack_record"] = round(time.time() - t, 1)
        timings["pack_reuse_stats"] = pack.stats
    if getattr(args, "packages", False):
        write_packages(args, out_dir, cars_out, timings, progress)
    if verify is not None:
        t = time.time()
        verify_pack(args, dsf, out_dir, work, verify[0], verify[1], port_args, port_options, results)
        timings["verify_pack"] = round(time.time() - t, 1)
    write_report(out_dir, archive, cars_out, failed, timings, started, args, results)
    emit(progress, "done", report=str(out_dir / "batch-report.json"))
    if getattr(args, "clean_work", False):
        for c in cars_out:
            (work / f"{c['media']}.chnk").unlink(missing_ok=True)


def take_whole_pack(pack, ok: list[dict], work: Path, timings: dict, progress) -> tuple[list, list] | None:
    """(the manifest's cars, the merge's failures) of an earlier pack of the same cars,
    settings, inputs and code, taken whole (pack_reuse.PackReuse.take_whole); else None,
    and `pack` (when there is one) starts recording this pack for later builds."""
    if pack is None:
        return None
    t = time.time()
    pack.prepare(ok, work)
    earlier = pack.whole()
    timings["pack_check"] = round(time.time() - t, 1)
    if earlier is None:
        pack.start_recording()
        return None
    print(f"pack: the same cars, settings, inputs and code as {earlier.folder}'s pack; reusing it")
    emit(progress, "pack_reused", source=str(earlier.folder), cars=len(ok))
    t = time.time()
    reused = pack.take_whole(earlier)
    timings["pack_reuse"] = round(time.time() - t, 1)
    return reused


def emit_failures(progress, failures: list[dict]) -> None:
    """A car_failed event for each car the merge left out."""
    for f in failures:
        emit(progress, "car_failed", media=f["media"], cause=f.get("cause"), error=f.get("error"))


def merge_built(
    args, dsf: Path, archive: Path, ok: list[dict], work: Path, merged: dict, timings: dict, progress
) -> tuple[list[dict], list[dict]]:
    """merge the built cars into `archive` (not written with --no-archive or past a CHNK's
    size; `merged` gets register_cars' info): (the manifest's cars, the cars left out)."""
    t = time.time()
    print(f"merging {len(ok)} cars into {archive}")
    emit(progress, "merge", cars=len(ok))
    cars_out, merge_failed = merge(
        dsf,
        archive,
        ok,
        work,
        root_limit=root_limit_of(args),
        write=not getattr(args, "no_archive", False),
        info=merged,
    )
    timings["merge"] = round(time.time() - t, 1)
    return cars_out, merge_failed


def lint_merged(
    args, dsf: Path, out_dir: Path, archive: Path, cars: list[dict], merged: dict, pack, timings: dict
) -> None:
    """upvh lint for the merged cars (lint_all sets each car's "lint"): from the archive, or
    from its merge plan (MERGE_PLAN) when it wasn't written; with `pack` only the cars
    whose lint can't be taken from an earlier pack (pack_reuse.PackReuse.lint)."""
    from dsfforza.build import pack_reuse

    progress = getattr(args, "progress", None)
    plan = out_dir / MERGE_PLAN
    if merged.get("written", True):
        plan.unlink(missing_ok=True)  # an earlier build's
        lint_source = archive
    else:  # too large for one CHNK (or --no-archive): lint merges the cars in memory
        lint_source = write_plan(plan, dsf, merged["plan"])
    t = time.time()
    parallel = lint_jobs(args.jobs)
    if pack is None:
        emit(progress, "lint", cars=len(cars), total=len(cars))
        lint_all(lint_source, dsf, cars, parallel)
    else:

        def run(some: list[dict], defined: set[int]) -> dict:
            print(f"lint: {len(some)} of {len(cars)} cars")
            emit(progress, "lint", cars=len(some), total=len(cars))
            return lint_all(
                lint_source,
                dsf,
                some,
                parallel,
                facts=True,
                defined=defined,
                records=pack.records,
                record_dir=out_dir / pack_reuse.FOLDER,
            )

        pack.lint(cars, run)
    timings["lint"] = round(time.time() - t, 1)


def report_lint(cars: list[dict], progress) -> None:
    """Name the cars that failed lint (they stay in the archive, listed as failed)."""
    lint_failed = [c for c in cars if c.get("lint") != "ok"]
    if lint_failed:
        print(f"{len(lint_failed)} car(s) failed lint; they stay in the archive but are listed as failed")
    for c in lint_failed:
        emit(progress, "lint_failed", media=c["media"], text=c.get("lint"))


def write_manifest(
    args, dsf: Path, out_dir: Path, archive: Path, cars: list[dict], failed: list[dict], port_args, port_options
) -> Path:
    """dngvehicles.json (port_car's manifest format, plus "batch": the ID policy, the
    options and the failures) and dngvehicles.lua (port_car.lua_snippet). Returns the
    manifest's path."""
    from dsfforza.build import pack_reuse, port_car

    manifest = {
        "archive": str(archive),
        "vanilla": str(dsf),
        "vanilla_sha256": pack_reuse.sha256_file(dsf).upper(),
        "host": port_args.host,
        "sound_host": port_args.sound_host,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "textures": port_args.textures,
        "renewed_uids": not port_args.share_host_uids,
        "cockpit": not port_args.no_cockpit,
        "cars": cars,
        "probe_ids": [c["spool_id"] for c in cars],
        "batch": {
            "id_policy": args.id_policy,
            "port_options": port_options,
            "failed": [{k: f.get(k) for k in ("media", "spool_id", "cause", "error")} for f in failed],
        },
    }
    manifest_path = out_dir / "dngvehicles.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, default=str) + "\n", encoding="utf-8")
    (out_dir / "dngvehicles.lua").write_text(port_car.lua_snippet(manifest), encoding="utf-8", newline="\n")
    return manifest_path


def build_gui(
    args, dsf: Path, out_dir: Path, archive: Path, manifest: Path, cars: list[dict], merged: dict, pack, timings: dict
) -> None:
    """--gui: gui.sp, Fonts.fchunk and hud.lua into out_dir/gui (gui_assets.build), the
    garage pictures drawn from the merged archive or, without it, from Shards. A failure
    leaves the build usable: it goes to timings["gui_error"] and an earlier gui/ is moved
    aside (move_stale_gui)."""
    from dsfforza.game import gui_assets

    progress = getattr(args, "progress", None)
    t = time.time()
    emit(progress, "gui", cars=len(cars))

    def gui_log(text=""):
        print(text)
        emit(progress, "gui_log", text=str(text))

    # Without the merged archive the renders read archives of some cars each (Shards).
    shards = None if archive.is_file() else Shards(dsf, merged["plan"], out_dir / SHARDS, log=gui_log)
    try:
        gui_assets.build(
            manifest,
            out_dir / "gui",
            log=gui_log,
            archive=archive if shards is None else shards,
            wide_glyphs=getattr(args, "wide_glyphs", False),
            # The same CHNK reader limit: with dsfcars.asi [archive] gui.sp gets one
            # SPOI per picture too, not shared SPOIs past 177 (research/gui-assets.md).
            root_limit=getattr(args, "root_limit", None) or ENTRY_LIMIT,
            jobs=render_jobs(args.jobs),
            # The disc images by title (logos, names): any file names, FH1 not needed.
            isos=gui_isos(args),
            cache=pack.picture_cache() if pack is not None else None,
            record=pack.records if pack is not None else None,
        )
    except Exception as error:  # noqa: BLE001 - reported; the archive is still usable without pictures
        print(f"gui assets failed: {type(error).__name__}: {error}")
        timings["gui_error"] = f"{type(error).__name__}: {error}"
        emit(progress, "gui_failed", error=timings["gui_error"])
        move_stale_gui(out_dir, timings)
    finally:
        if shards is not None:
            shards.close()
    timings["gui"] = round(time.time() - t, 1)


def move_stale_gui(out_dir: Path, timings: dict) -> None:
    """After a failed GUI step: an earlier run's gui/ (or this run's half) doesn't match the
    new archive's IDs, and install.ps1 installs whatever gui/ holds, so it is moved aside to
    gui-stale-<time>/ (timings["gui_stale"]); nothing is deleted."""
    gui = out_dir / "gui"
    if not gui.exists():
        return
    stale = out_dir / f"gui-stale-{datetime.datetime.now():%Y%m%d-%H%M%S}"
    try:
        gui.rename(stale)
        print(f"moved {gui} to {stale.name}: it doesn't match this archive")
        timings["gui_stale"] = str(stale)
    except OSError as move_error:
        print(f"warning: {gui} doesn't match this archive and could not be moved: {move_error}")


PACKAGES = "packages"  # <build>/packages/<key>.dsfcar (--packages)


def write_packages(args, out_dir: Path, cars: list[dict], timings: dict, progress) -> None:
    """--packages: one .dsfcar per car (src/dsfforza/build/dsfcar.py) in <build>/packages, from
    work/<media>.chnk, the manifest, and the pack step's pictures and images (so after
    pack.save and before --clean-work). They are written to packages.new and swapped in
    whole, so <build>/packages is always one build's complete set. On a failure the build
    stands (as for the GUI step), but the packages go, the earlier ones included (they don't
    match this build), and the report says why (timings packages_error): install.ps1 and
    dsff install refuse such a build (they install only packages)."""
    import shutil

    t = time.time()
    folder = out_dir / PACKAGES
    staged = out_dir / (PACKAGES + ".new")
    shutil.rmtree(staged, ignore_errors=True)
    emit(progress, "packages", cars=len(cars))
    try:
        written = pack_all(out_dir, [c["media"] for c in cars], staged, gui_isos(args))
        shutil.rmtree(folder, ignore_errors=True)
        staged.replace(folder)
        timings["packages"] = len(written)
    except Exception as error:  # noqa: BLE001 - reported; the archive and gui/ are still usable
        shutil.rmtree(staged, ignore_errors=True)
        shutil.rmtree(folder, ignore_errors=True)
        print(f"packages failed: {type(error).__name__}: {error} (no packages for this build)")
        timings["packages_error"] = f"{type(error).__name__}: {error}"
        emit(progress, "packages_failed", error=timings["packages_error"])
    timings["packages_s"] = round(time.time() - t, 1)


def pack_all(out_dir: Path, media: list[str], folder: Path, isos: list[Path]) -> list[Path]:
    """dsfcar.pack for every car, or ValueError when a car got no package."""
    from dsfforza.build import dsfcar

    written = dsfcar.pack(out_dir, media, folder, isos=isos)
    if len(written) != len(media):
        raise ValueError(f"{len(written)} packages for {len(media)} cars")
    return written


def gui_isos(args) -> list[Path]:
    """The disc images the GUI step and the packages read: --iso, else assets/*.iso."""
    return [Path(i) for i in (getattr(args, "iso", None) or forza_roster.default_images())]


def pack_outputs(out_dir: Path, cars: list[dict], *, gui: bool) -> list[str]:
    """The files a pack of `cars` wrote, relative to out_dir (dngvehicles.json and .lua are
    written every time): the archive, or its merge plan when it wasn't written, gui/ (not
    the previews of cars no longer in it) and pack/pictures/."""
    from dsfforza.build import pack_reuse

    names = {f"dsfforza_{c['media'].lower()}" for c in cars}
    out = ["dngvehicles.sp", "dngvehicles.json", MERGE_PLAN]
    folder = out_dir / "gui"
    if gui and folder.is_dir():
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            if path.parent == folder and path.name.startswith("dsfforza_") and path.stem not in names:
                continue
            out.append(path.relative_to(out_dir).as_posix())
    pictures = out_dir / pack_reuse.FOLDER / pack_reuse.PICTURES
    out += [
        f"{pack_reuse.FOLDER}/{pack_reuse.PICTURES}/{n}.dds" for n in sorted(names) if (pictures / f"{n}.dds").is_file()
    ]
    return out


VERIFY_FOLDER = "pack-verify"


def verify_pack(args, dsf, out_dir, work, ok, failed, port_args, port_options, results) -> None:
    """--verify-pack: a full pack of the same cars into out_dir/pack-verify (nothing
    reused) must give the same files as this one; raise SystemExit when any differs."""
    import shutil

    folder = out_dir / VERIFY_FOLDER
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    print(f"--verify-pack: a full pack into {folder} to compare")
    check = argparse.Namespace(**{**vars(args), "progress": None, "clean_work": False, "reuse_record": None})
    from dsfforza.game import gui_assets

    gui_assets._TEXT_PLATES.clear()  # drawn again, not taken from the pack being checked
    finish(check, dsf, folder, work, ok, failed, port_args, port_options, {}, time.time(), results, pack=None)
    diffs = compare_packs(out_dir, folder, gui=args.gui)
    if diffs:
        raise SystemExit(
            f"--verify-pack: the pack differs from a full pack of the same cars in {len(diffs)} file(s): "
            f"{'; '.join(diffs[:8])}. The full pack is in {folder}; build with --no-pack-reuse, and see "
            "research/batch.md, 'Pack reuse'."
        )
    print("--verify-pack: identical to a full pack")
    shutil.rmtree(folder, ignore_errors=True)


def compare_packs(mine: Path, full: Path, *, gui: bool) -> list[str]:
    """What differs between two packs of the same cars: the archive, the Lua, the manifest
    (but its time and archive path) and every file of the full pack's gui/."""
    from dsfforza.build import pack_reuse

    diffs = []

    def same(a: Path, b: Path) -> bool:
        if not a.exists() and not b.exists():  # neither wrote it (dngvehicles.sp past 4 GiB)
            return True
        if not a.is_file() or not b.is_file() or a.stat().st_size != b.stat().st_size:
            return False
        return pack_reuse.sha256_file(a) == pack_reuse.sha256_file(b)

    for rel in ("dngvehicles.sp", "dngvehicles.lua"):
        if not same(mine / rel, full / rel):
            diffs.append(rel)

    def manifest(folder: Path) -> dict:
        data = json.loads((folder / "dngvehicles.json").read_text(encoding="utf-8"))
        data.pop("created", None)
        data.pop("archive", None)
        return data

    if manifest(mine) != manifest(full):
        diffs.append("dngvehicles.json")
    if gui:
        for path in sorted((full / "gui").rglob("*")):
            if path.is_file():
                rel = path.relative_to(full).as_posix()
                if not same(mine / rel, path):
                    diffs.append(rel)
        expected = set(pack_outputs(full, json.loads((full / "dngvehicles.json").read_text())["cars"], gui=gui))
        extra = [
            rel
            for rel in pack_outputs(mine, json.loads((mine / "dngvehicles.json").read_text())["cars"], gui=gui)
            if rel.startswith("gui/") and rel not in expected
        ]
        diffs += [f"{rel} (not in the full pack)" for rel in extra]
    return diffs


def cmd_remerge(args) -> None:
    """A new archive from an earlier build's work/ folder under another ID policy:
    nothing is converted again, only the vehicle ID fields are rewritten."""
    started = time.time()
    set_game_env(args.dsf)
    lower_priority()
    source = args.source
    work = source / "work"
    old = json.loads((source / "dngvehicles.json").read_text(encoding="utf-8"))
    ok = []
    for c in sorted(old["cars"], key=lambda c: c["spool_id"]):
        report = json.loads((work / f"{c['media']}.json").read_text(encoding="utf-8"))
        if report.get("ok"):
            ok.append(report)
    _, taken = vanilla_ids(args.dsf)
    picks = [Pick(r["media"], r["game"], None, "", {}) for r in ok]
    assign_ids(picks, taken, args.id_policy, args.first_id)
    for r, p in zip(ok, picks):
        r["new_id"] = p.spool_id
    port_options = old.get("batch", {}).get("port_options", [])
    port_args = port_car_args(["--dsf", str(args.dsf), "--out", "x.sp", "--car", "X"] + port_options)
    args.jobs = gentle.jobs(args.jobs or 4)
    args.out.mkdir(parents=True, exist_ok=True)
    failed = list(old.get("batch", {}).get("failed", []))
    finish(
        args, args.dsf, args.out, work, ok, failed, port_args, port_options, {}, started, {r["media"]: r for r in ok}
    )


def write_report(
    out_dir: Path,
    archive: Path,
    cars: list[dict],
    failed: list[dict],
    timings: dict,
    started: float,
    args,
    results: dict | None = None,
) -> None:
    """batch-report.json (per car its host, lint, fallback, time, LODs, sizes and engine
    sound; the failures by cause; the reuse record) and the summary printed at the end of
    a build."""
    from dsfforza.convert import engine_sound

    timings["total"] = round(time.time() - started, 1)
    causes: dict[str, list[str]] = {}
    for f in failed:
        causes.setdefault(f.get("cause") or "?", []).append(f["media"])
    sounds = {c["media"]: engine_sound.summary(c.get("forza_sound") or {}) for c in cars}
    report = {
        "archive": str(archive),
        "archive_bytes": archive.stat().st_size if archive.exists() else None,
        "jobs": args.jobs,
        "timings_s": timings,
        "ok": [
            {
                "spool_id": c["spool_id"],
                "media": c["media"],
                "game": c["game"],
                "host": c["host"],
                "host_name": c["host_name"],
                "lint": c.get("lint"),
                "fallback": c.get("batch_fallback"),
                "seconds": (results or {}).get(c["media"], {}).get("seconds"),
                "lods": c.get("lods"),
                "sizes": c.get("sizes"),
                "sound": sounds[c["media"]],
            }
            for c in cars
        ],
        "failed": [{k: f.get(k) for k in ("media", "spool_id", "cause", "error")} for f in failed],
        "failed_by_cause": causes,
        "forza_sound": {
            "cars": sum(s["forza"] for s in sounds.values()),
            "host_sound": {m: s["reason"] for m, s in sounds.items() if not s["forza"]},
            "previews": str(out_dir / "sound"),
        },
        "fallbacks": {
            c["media"]: {
                "fallback": c.get("batch_fallback"),
                "first_cause": (results or {}).get(c["media"], {}).get("first_cause"),
            }
            for c in cars
            if c.get("batch_fallback")
        },
    }
    if getattr(args, "reuse_record", None):
        report["reuse"] = args.reuse_record
    (out_dir / "batch-report.json").write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    print(
        f"\n{len(cars)} cars built, {len(failed)} failed; total {timings['total']:.0f} s "
        f"({', '.join(f'{k} {v}' for k, v in timings.items() if k != 'total')})"
    )
    for cause, names in sorted(causes.items()):
        print(f"  {cause}: {', '.join(names)}")
    for media, f in report["fallbacks"].items():
        print(f"  built {f['fallback']}: {media} (first try: {f['first_cause']})")
    if cars:
        print(f"  Forza engine sound: {report['forza_sound']['cars']} of {len(cars)} cars")
        for media, reason in report["forza_sound"]["host_sound"].items():
            print(f"  host sound: {media} ({reason})")
    if archive.exists():
        print(f"archive {archive} ({archive.stat().st_size / 2**20:.0f} MiB), report {out_dir / 'batch-report.json'}")


ROSTER = REPO / "tools" / "batches" / "all.txt"
ROSTER_ROOT_LIMIT = 3003  # install.ASI_ROOT_LIMIT: every base ID below 1000 plus SSIC, DMWK, BINF


def roster_selection(args) -> None:
    """--roster: the cars and IDs of dsff install's build (tools/batches/all.txt, then the FM4
    DLC packs' cars; --id-policy asi, --root-limit 3003), so a subset (--affected) gets the
    IDs a full build gave its cars, and the car store's full builds match it."""
    if getattr(args, "roster", False):
        args.list = [*(args.list or []), ROSTER]
        args.fm4_dlc_cars = True
        args.id_policy = "asi"
        args.root_limit = ROSTER_ROOT_LIMIT


def _change_arguments(p: argparse.ArgumentParser, name: str) -> None:
    """affected's and build's options for a change since --base (car store)."""
    p.add_argument(
        "--roster",
        action="store_true",
        help="the cars and IDs of dsff install's build: --list tools/batches/all.txt --fm4-dlc-cars "
        "--id-policy asi --root-limit 3003",
    )
    if name not in ("affected", "build"):
        return
    p.add_argument(
        "--base",
        metavar="REV",
        help="the revision a change is measured from (default: the merge base of HEAD and origin/master)",
    )
    if name == "affected":
        p.add_argument("--write-list", type=Path, help="write the affected and unknown cars as a --list file")
        p.add_argument("--json", type=Path, help="write {media: status, why, entry}")
        return
    p.add_argument(
        "--affected",
        action="store_true",
        help="build only the cars a change since --base affects (port_batch affected), with the "
        "IDs of the whole selection",
    )
    p.add_argument(
        "--before",
        action="store_true",
        help="every car from the car store as --base's code made it, none converted (the 'before' "
        "of a change; with --affected, of the cars it affects)",
    )


def build_parser() -> argparse.ArgumentParser:
    """The plan / build / remerge command line (src/dsfforza/build/install.py builds its
    port_batch arguments with it too)."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "affected", "build"):
        p = sub.add_parser(name)
        p.add_argument(
            "--dsf",
            type=Path,
            default=None,
            help="vanilla Install/dngvehicles.sp (only read; default: from $DSF_GAME_DIR); "
            "its game folder is used for the rest",
        )
        p.add_argument(
            "--iso", type=Path, action="append", help="Forza disc images, any file names (default: assets/*.iso)"
        )
        p.add_argument("--cache", type=Path, default=REPO / "out" / "cache", help="extraction cache")
        p.add_argument("--car", action="append", metavar="NAME[@fh1|@fm4]")
        p.add_argument("--list", type=Path, action="append", help="file with one car per line")
        p.add_argument(
            "--fm4-dlc-cars",
            action="store_true",
            help="also every car of tools/batches/fm4-dlc.txt that the FM4 DLC packs found have",
        )
        p.add_argument(
            "--no-fm4-dlc",
            action="store_true",
            help="leave the FM4 DLC packs under assets/ out (roster, gamedb; src/dsfforza/build/fm4_dlc.py)",
        )
        p.add_argument("--game", choices=("fh1", "fm4", "FH1", "FM4"))
        p.add_argument("--make", help="comma-separated make codes or names")
        p.add_argument("--body", help="comma-separated body classes")
        p.add_argument("--year", help="range, e.g. 1960-1979")
        p.add_argument("--no-counterpart", action="store_true", help="only cars DSF has no same-model car for")
        p.add_argument("--counterpart", action="store_true", help="only cars DSF has a same-model car for")
        p.add_argument("--exclude", action="append", metavar="NAME")
        p.add_argument("--diverse", type=int, metavar="N", help="N cars spread over body classes and makes")
        p.add_argument("--max", type=int)
        p.add_argument(
            "--id-policy",
            choices=("garage", "high", "asi"),
            default="garage",
            help="garage (default): IDs 1..61 first, then --first-id up; high: --first-id up only; "
            "asi: every free ID from 1 (vanilla gaps in 62..303 too), then --first-id up, garage-listed "
            "through dsfcars.asi (research/asi.md)",
        )
        p.add_argument("--first-id", type=int, default=HIGH_FIRST_ID)
        p.add_argument(
            "--root-limit",
            type=int,
            default=ENTRY_LIMIT,
            help=f"root entries allowed (default {ENTRY_LIMIT}, the vanilla reader); "
            "more needs dsfcars.asi [archive], e.g. 3003 for every base ID below 1000",
        )
        _change_arguments(p, name)
        if name == "plan":
            p.add_argument("--tsv", type=Path)
        elif name == "build":
            p.add_argument("--out", type=Path, required=True, help="output folder")
            p.add_argument(
                "--jobs",
                type=int,
                default=default_jobs(),
                help="worker processes of this build at most (default: one per logical CPU, as many as "
                f"{WORKER_RAM / (1 << 30):.1f} GB each fits in the RAM: {default_jobs()} here; out/gentle caps it "
                "at 2). Every build and survey on the PC takes its slots from one job pool, as its RAM budget "
                "allows (jobpool.py)",
            )
            p.add_argument("--port", help=f"port_car options for every car (default: {DEFAULT_PORT_OPTIONS!r})")
            p.add_argument("--gui", action="store_true", help="also build gui.sp, Fonts.fchunk and hud.lua")
            p.add_argument(
                "--packages",
                action="store_true",
                help="also write one car package per car to <out>/packages (<key>.dsfcar; with --gui for "
                "their garage pictures; what install.ps1 installs; research/car-packages.md)",
            )
            p.add_argument(
                "--no-archive",
                action="store_true",
                help="don't write the merged dngvehicles.sp (the car packages don't need it; it is never "
                "written past 4 GiB, a CHNK's limit): lint merges the cars in memory and the garage "
                "pictures render from smaller archives",
            )
            p.add_argument(
                "--wide-glyphs",
                action="store_true",
                help="with --gui: every car its own HUD badge/plate as 16-bit glyphs "
                "(needs dsfcars.asi [hud]; gui_assets.py --wide-glyphs)",
            )
            p.add_argument(
                "--resume",
                action="store_true",
                help="reuse cars an earlier run in --out built with the same ID and options",
            )
            p.add_argument(
                "--reuse",
                type=Path,
                action="append",
                metavar="DIR",
                help="copy each car an earlier build in DIR made from the same job, input files and code "
                "(work/<media>.fp.json) instead of converting it again (research/batch.md, 'Selective rebuild'); "
                "repeat to reuse from several builds (the first with a match wins)",
            )
            p.add_argument(
                "--rebuild-where",
                metavar="EXPR",
                help="with --reuse: also build the cars whose earlier report matches this Python expression "
                "(names: r = the work/<media>.json report, entry, media, game, host), e.g. "
                '"\'cockpit\' in entry" or "host == 198"',
            )
            p.add_argument(
                "--trust-code",
                action="store_true",
                help="with --reuse: ignore code changes (only inputs and --rebuild-where decide); for a fix "
                "whose affected cars --rebuild-where names",
            )
            p.add_argument(
                "--verify-reuse",
                type=int,
                default=None,
                metavar="N",
                help="build N random reused cars again and stop if any differs (default 2 with --reuse, "
                "0 for cars from the car store alone; 0: off)",
            )
            p.add_argument("--verify-seed", type=int, help="the random seed of --verify-reuse (default: random)")
            p.add_argument(
                "--verify-mismatch",
                choices=("stop", "rebuild"),
                default="stop",
                help="when --verify-reuse finds a car that differs: stop the build (default), or use the fresh "
                "copy and convert the other cars reused from the same build again (dsff install)",
            )
            p.add_argument(
                "--no-store",
                action="store_true",
                help="neither take cars from the car store all worktrees share nor put this build's in "
                "(src/dsfforza/build/carstore.py; DSFFORZA_STORE=off does the same)",
            )
            p.add_argument(
                "--no-pack-reuse",
                action="store_true",
                help="always merge, lint and draw every car (default: take what didn't change from this "
                "folder's and the --reuse builds' packs; src/dsfforza/build/pack_reuse.py)",
            )
            p.add_argument(
                "--verify-pack",
                action="store_true",
                help="after the pack, make a full pack into <out>/pack-verify and stop if any file differs",
            )
            p.add_argument(
                "--clean-work",
                action="store_true",
                help="delete the per-car chunk files afterwards (default: keep them for remerge)",
            )
    r = sub.add_parser("remerge", help="re-assemble an earlier build under another ID policy (no conversion)")
    r.add_argument("source", type=Path, help="an earlier build's --out folder (with work/)")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--dsf", type=Path, default=None, help="default: from $DSF_GAME_DIR")
    r.add_argument("--id-policy", choices=("garage", "high", "asi"), default="high")
    r.add_argument("--first-id", type=int, default=HIGH_FIRST_ID)
    r.add_argument(
        "--root-limit",
        type=int,
        default=ENTRY_LIMIT,
        help=f"root entries allowed (default {ENTRY_LIMIT}; more needs dsfcars.asi [archive])",
    )
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--gui", action="store_true")
    r.add_argument("--wide-glyphs", action="store_true", help="with --gui: 16-bit HUD glyphs (dsfcars.asi [hud])")
    return parser


def main(argv: list[str] | None = None) -> None:
    """port_batch's command line. `_worker JOB` runs one car's worker process (run_workers
    starts them)."""
    argv = sys.argv[1:] if argv is None else argv
    safe_stdio()  # python -m dsfforza.build.port_batch, and the _worker processes
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
    if argv[:1] == ["_worker"]:
        sys.exit(worker(Path(argv[1])))
    args = build_parser().parse_args(argv)
    roster_selection(args)
    args.dsf = game_paths.vanilla(args.dsf)  # the original when a dsfforza install replaced it
    {"plan": cmd_plan, "affected": cmd_affected, "build": cmd_build, "remerge": cmd_remerge}[args.cmd](args)


if __name__ == "__main__":
    main()
