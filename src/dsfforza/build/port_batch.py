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
Normal priority, at most --jobs at a time, a new one only while 1.5 GB of RAM
is free), which writes the car's three VEHC entries to work/<media>.chnk and
a report. The merge then registers them into one copy of the vanilla archive
(SSLP/VSCT records), checks the root entry limit (--root-limit, default 1100:
150 vanilla cars plus at most 215 new ones; dsfforza.asi's archive module lifts
it, research/spool-limits.md) and UID clashes (a car that clashes is left out
and the rest merged again), writes the archive, and lints every car.
A car that fails in src/dsfforza/convert/damage.py is retried once without --damage, one
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

ID policy (research/batch.md): the garage lists model IDs 0..303 only
(research/gui-assets.md) and vanilla cars use 62..303, so the first cars of a
batch get IDs 1..61 (garage-visible), the rest 304 upwards (traffic, Shift and
scripts, but never the garage). --id-policy high puts every car at 304+
(the range confirmed in game); --first-id moves the high range.
--id-policy asi is for installs with dsfforza.asi (tools/asi, research/asi.md):
its garage module lists IDs up to 999 in up to 120 maker groups and keeps the
save flags of IDs 304+ in a side file, so every car can be in the garage. Cars
get every free ID in order: 1..61, the 92 IDs in 62..303 the vanilla archive
leaves unused, then --first-id up; cars that won't fit a maker group get a note.

Outputs in --out DIR: dngvehicles.sp, dngvehicles.json (port_car's manifest
format, plus "batch": failures and timings), dngvehicles.lua (the
dsfforza\\ported.lua for register.lua), batch-report.json (per car its
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
FREE_RAM_FOR_WORKER = 1536 << 20
BELOW_NORMAL = 0x00004000
DEFAULT_PORT_OPTIONS = "--mdxn-limit 0x2C0000 --damage"


# --- environment ------------------------------------------------------------------------


def game_from_dsf(dsf: Path) -> Path:
    game = dsf.resolve().parent.parent
    # <game>/dsfforza-backup/Install/dngvehicles.sp (src/dsfforza/game/game_paths.py): the game is one up.
    if game == game_paths.REPO_VANILLA.resolve():  # the repo's vanilla copy (a junction in a worktree): the real game
        return game_paths.game_dir()
    return game.parent if game.name == game_paths.BACKUP else game


def set_game_env(dsf: Path) -> None:
    """Point every tool at this game folder (host_select, gui_assets read DSF_GAME_DIR
    when imported, so call this before importing them)."""
    os.environ["DSF_GAME_DIR"] = str(game_from_dsf(dsf))


def free_ram() -> int | None:
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
    return status.avail


def lower_priority() -> None:
    """This process at Below Normal (workers are started that way too)."""
    try:
        if os.name == "nt":
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetPriorityClass(handle, BELOW_NORMAL)
        else:
            os.nice(5)
    except (OSError, AttributeError):
        pass
    gentle.apply()  # out/gentle: Idle on two CPUs (src/dsfforza/build/gentle.py)


def emit(progress, event: str, **data) -> None:
    """Report a build event to `progress` (args.progress, set by a caller such as
    src/dsfforza/build/install.py; None from the command line). Events: plan, convert,
    car_start, car_retry, car_done, verify, merge, lint, gui, gui_log, done."""
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
    from dsfforza.build.forza_roster import roster

    cars, _ = roster(isos)
    return {k: c for k, c in cars.items() if c.kind == "car"}


def gamedbs(isos: list[Path], cache: Path) -> dict[str, Path]:
    from dsfforza.build import port_car

    out = {}
    for game in ("FH1", "FM4"):
        db = port_car.extract_gamedb(isos, game, cache)
        if db:
            out[game] = db
    return out


def describe_cars(
    cars: dict, requests: list[tuple[str, str | None]], dsf: Path, dbs: dict[str, Path], cache: Path | None = None
) -> list[Pick]:
    """A Pick per (media, game or None) with body class, best host and the vanilla
    counterpart; unknown names become picks with a note. The host is picked as
    port_car.pick_hosts picks it: the source game's gamedb and the car's extracted
    folder under `cache` (its carbin's door count, issue #239)."""
    from dsfforza.build import host_select as hs

    hosts = hs.survey(dsf)
    memo = hs.door_memo()
    try:
        return _describe(cars, requests, dbs, cache, hosts, memo)
    finally:
        memo.save()


def _describe(cars: dict, requests, dbs: dict[str, Path], cache: Path | None, hosts: list, memo) -> list[Pick]:
    from dsfforza.build import host_select as hs
    from dsfforza.build.port_car import CarRequest, car_folder, pick_source

    picks = []
    for media, game in requests:
        car = cars.get(media.lower())
        if car is None:
            picks.append(Pick(media, game or "?", None, "", {}, notes=["not a car on the discs"]))
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
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            media, _, game = line.split()[0].partition("@")
            out.append((media, game.upper() or None))
    return out


def select(args, cars: dict, dsf: Path, dbs: dict[str, Path]) -> list[Pick]:
    explicit = [(m, g.upper() or None) for m, _, g in (c.partition("@") for c in args.car or [])]
    for path in args.list or []:
        explicit += read_list(path)
    if explicit:
        requests = explicit
    else:
        requests = [
            (c.name, args.game.upper() if args.game else None)
            for c in sorted(cars.values(), key=lambda c: c.name.lower())
            if not args.game or args.game.upper() in c.games.split(",")
        ]
    picks = describe_cars(cars, requests, dsf, dbs, args.cache)
    excluded = {e.lower() for e in args.exclude or []}
    makes = {m.strip().lower() for m in (args.make or "").split(",") if m.strip()}
    bodies = {b.strip().lower() for b in (args.body or "").split(",") if b.strip()}
    years = None
    if args.year:
        lo, _, hi = args.year.partition("-")
        years = (int(lo or 0), int(hi or 9999))
    out = []
    for p in picks:
        if p.media.lower() in excluded:
            continue
        if not explicit:  # filters apply to the roster; a named list is taken as given
            if p.notes:
                continue
            if args.game and p.game != args.game.upper():
                continue
            if makes and p.make.lower() not in makes and p.media.split("_")[0].lower() not in makes:
                continue
            if bodies and p.body not in bodies:
                continue
            if not bodies and p.body == "race":
                continue
            if years and not (p.year and years[0] <= p.year <= years[1]):
                continue
            if args.no_counterpart and p.counterpart is not None:
                continue
            if args.counterpart and p.counterpart is None:
                continue
        out.append(p)
    if args.diverse:
        out = diverse(out, args.diverse)
    if args.max:
        out = out[: args.max]
    seen = set()
    unique = []
    for p in out:
        if p.media.lower() not in seen:
            seen.add(p.media.lower())
            unique.append(p)
    return unique


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
ASI_GROUPS_CAP = 120 - 1  # dsfforza.asi [garage] max_groups (default 120)
ASI_GROUP_CAP = 1000  # dsfforza.asi [garage] max_cars: a maker group holds all its cars
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


OVERFLOW_GROUP = "FORZA"  # register.lua: makes without a garage group of their own share these


def garage_group(make: str, group: str, counts: dict[str, int], cap: int, groups_cap: int) -> str | None:
    """The garage maker group a car joins, or None: its own group while it has room (a new
    make opens one while fewer than groups_cap exist), else a shared FORZA, FORZA 2 ...
    group (the same rule as register.lua's garageGroup). One group slot is kept for FORZA."""
    if group in counts:
        if counts[group] < cap:
            return group
    elif len(counts) + (0 if OVERFLOW_GROUP in counts else 1) < groups_cap:  # keep room for FORZA
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
    """--root-limit: 1100 (vanilla) unless the install uses dsfforza.asi's archive module."""
    limit = getattr(args, "root_limit", None) or ENTRY_LIMIT
    if limit > ENTRY_LIMIT:
        print(
            f"note: root limit {limit}: the archive needs dsfforza.asi with [archive] enabled "
            f"(the vanilla game reads {ENTRY_LIMIT} entries)",
            file=sys.stderr,
        )
    return limit


def assign_ids(
    picks: list[Pick], taken: set[int], policy: str, first_high: int, isos: list[Path] | None = None
) -> None:
    """policy "garage": IDs 1..61 (listed in the garage) while a maker group has room
    for the car, else first_high and up; "high": first_high and up only; "asi"
    (installs with dsfforza.asi): every free ID below 1000 in order, 1..61, the 92
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
        group = garage_group(own, own, counts, GARAGE_GROUP_CAP, GARAGE_GROUPS_CAP) if low else None
        if group is not None:
            p.spool_id = low.pop(0)
            counts[group] = counts.get(group, 0) + 1
            if group != own:
                p.notes_soft = f"garage group {group}"
            continue
        if low:
            p.notes_soft = "no garage group left"
        if policy == "asi" and own is not None:
            group = garage_group(own, own, counts, ASI_GROUP_CAP, ASI_GROUPS_CAP)
            if group is None:
                p.notes_soft = "no garage group left (dsfforza.asi)"
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
    from the innermost frame in src/dsfforza/ of the traceback."""
    import re

    for needle, cause in CAUSES:
        if needle in text:
            return cause
    frames = re.findall(r'dsfforza[\\/](?:\w+[\\/])?(\w+)\.py", line \d+', text)
    frames = [f for f in frames if f not in ("vehicle_mesh", "chnk")] or frames  # the step, not a helper
    error = re.findall(r"^(\w+(?:Error|Exception|Exit)):", text, re.M)
    if frames:
        return f"{frames[-1]}.py: {error[-1] if error else 'error'}"
    return "conversion error"


def fallback_for(report: dict, options: list[str]) -> tuple[list[str], str] | None:
    """Options for one retry after a failure that a lesser build avoids: without
    --damage when src/dsfforza/convert/damage.py failed, with --no-cockpit (the host's interior)
    when src/dsfforza/convert/cockpit.py or the cockpit carbin failed. None: no retry, also for a
    worker that timed out or died without a report (its "error" is the log tail,
    which names every step the car got through, so it says nothing about the cause)."""
    if report.get("cause") in WORKER_DEATHS:
        return None
    text = (report.get("traceback") or "") + (report.get("error") or "")
    if "damage.py" in text and "--damage" in options:
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
        from dsfforza.formats.chnk import read_chunk, write_chunk

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
            root = read_chunk(stream, 0)
            entries = root.entries
            nonr_limit, mdxn_max = port_car.vanilla_limits(stream, entries, args.mdxn_limit)
            names = {s: entries[i].name for s, i, k in clone_vehicle.sslp_records(stream, entries) if k == 0}
            if job.get("sound_dir"):
                args.forza_sound_dir = Path(job["sound_dir"])
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
    except BaseException as error:  # SystemExit from port_car included
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
        except Exception as error:  # no fingerprint: a later --reuse builds the car again
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
    """Run one worker process per job, at most `parallel` at once, Below Normal priority.
    A worker that can't start, times out or dies is a failed car, not a failed batch;
    workers still running when this exits early (an error, Ctrl+C) are stopped.
    `progress` gets car_start, car_retry and car_done events (emit)."""
    pending = list(jobs)
    jobs_by_media = {j["media"]: j for j in jobs}
    running: dict[str, tuple[subprocess.Popen, float, object]] = {}
    results: dict[str, dict] = {}
    flags = gentle.creation_flags(BELOW_NORMAL if os.name == "nt" else 0)
    try:
        while pending or running:
            while pending and len(running) < parallel:
                ram = free_ram()
                if running and ram is not None and ram < FREE_RAM_FOR_WORKER:
                    break
                job = pending.pop(0)
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
                except OSError as error:
                    log_file.close()
                    results[job["media"]] = {
                        "media": job["media"],
                        "spool_id": job["spool_id"],
                        "ok": False,
                        "cause": "worker crash",
                        "error": f"could not start: {error}",
                    }
                    log(f"  FAILED to start {job['media']}: {error}")
                    emit(progress, "car_done", media=job["media"], report=results[job["media"]], log=None)
                    continue
                running[job["media"]] = (proc, time.time(), log_file)
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
                report = worker_report(work, media, code)
                report.setdefault("spool_id", jobs_by_media[media]["spool_id"])
                report["seconds"] = round(time.time() - started, 1) + jobs_by_media[media].get("seconds_before", 0)
                job = jobs_by_media[media]
                retry = (
                    None
                    if report.get("ok") or job.get("fallback") or job.get("verify")
                    else fallback_for(report, job["port_options"])
                )
                if retry:
                    options, why = retry
                    for ext in ("json", "log", "fp.json"):  # a crashed worker leaves no report
                        if (work / f"{media}.{ext}").exists():
                            (work / f"{media}.{ext}").replace(work / f"{media}.first-try.{ext}")
                    job = {
                        **job,
                        "port_options": options,
                        "fallback": why,
                        "first_error": report.get("error"),
                        "first_cause": report.get("cause"),
                        "seconds_before": report["seconds"],
                    }
                    jobs_by_media[media] = job
                    pending.append(job)
                    log(f"  retry {media} {why}")
                    emit(progress, "car_retry", media=media, why=why, cause=report.get("cause"))
                    continue
                if job.get("fallback"):
                    report.update(
                        {
                            "fallback": job["fallback"],
                            "first_error": job.get("first_error"),
                            "first_cause": job.get("first_cause"),
                        }
                    )
                results[media] = report
                state = "ok" if report.get("ok") else f"FAILED ({report.get('cause')}: {report.get('error', '')[:160]})"
                log(
                    f"  done  {media} in {report['seconds']:.0f} s: {state}"
                    + (f" ({job['fallback']})" if job.get("fallback") else "")
                )
                emit(progress, "car_done", media=media, report=report, log=str(work / f"{media}.log"))
    finally:
        for proc, _, log_file in running.values():  # only when leaving early
            proc.kill()
            proc.wait()
            log_file.close()
    return results


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
    dsf: Path, out: Path, results: list[dict], work: Path, log=print, root_limit: int = ENTRY_LIMIT
) -> tuple[list[dict], list[dict]]:
    """Register every built car into a copy of the vanilla archive. Returns (entries
    of the manifest's "cars", failures found here). Cars that define a resource or
    MODL part UID another vehicle defines too are left out (cause "UID clash") and
    the rest are registered again, so one clash doesn't fail the whole batch."""
    failed: list[dict] = []
    while True:
        cars, found, clashing = register_cars(dsf, out, results, work, log, root_limit)
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
    dsf: Path, out: Path, results: list[dict], work: Path, log=print, root_limit: int = ENTRY_LIMIT
) -> tuple[list[dict], list[dict], dict[str, list[str]]]:
    """One merge pass: (manifest "cars", failures, {media: clashing UIDs}). The
    archive is written only when no car clashes."""
    from dsfforza.build import clone_vehicle, port_car
    from dsfforza.formats.chnk import read_chunk, write_chunk

    stream = MultiStream(dsf)
    failed = []
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
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_name(out.name + ".tmp")
        try:
            with temp.open("wb") as handle:
                write_chunk(handle, stream, entries, root.version)
            Path(temp).replace(out)
        finally:
            temp.unlink(missing_ok=True)  # a failed write leaves no 1-2 GB partial copy behind
        size = out.stat().st_size
        if size >= 1 << 31:
            # The vanilla file layer clamps seek positions of 2 GiB and up to 0 (spool-limits.md).
            log(
                f"note: {out.name} is {size / 2**30:.2f} GiB; past 2 GiB it needs dsfforza.asi "
                f"[archive] large_files=1 (the default)"
            )
    finally:
        stream.close()
    return cars, failed, {}


def plain_glass(car: dict) -> bool:
    """True when the car's converted cockpit has interior glass but no window that
    qualified for crack lines (glass.InteriorGlass leaves lamp covers and small panes
    as surface only: open race cars and roadsters), as its cockpit report says."""
    interior = (car.get("cockpit") or {}).get("interior")
    windows = interior.get("glass_windows") if isinstance(interior, dict) else None
    return bool(windows) and not any("crack_m" in w for w in windows.values())


LINT_GROUP = 64  # cars per lint process at least: each reads the whole archive's tables first (~20 s)


def lint_all(
    out: Path,
    dsf: Path,
    cars: list[dict],
    parallel: int,
    log=print,
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
    the records are appended (src/dsfforza/build/pack_reuse.py)."""
    import json

    flags = gentle.creation_flags(BELOW_NORMAL if os.name == "nt" else 0)
    processes = max(1, min(parallel, -(-len(cars) // LINT_GROUP)))
    size = -(-len(cars) // processes) if cars else 1
    groups = [cars[k : k + size] for k in range(0, len(cars), size)]
    procs = []
    for n, group in enumerate(groups):
        command = [sys.executable, "-m", "dsfforza.formats.upvh", "lint-many", str(out), "--vanilla", str(dsf)]
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
    set_game_env(args.dsf)
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
            except Exception as error:  # names then come from the media names
                print(f"warning: {game} string tables from {iso.name}: {type(error).__name__}: {error}")


def print_plan(picks: list[Pick]) -> None:
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
    dsf, isos, cars, dbs = common(args)
    picks = select(args, cars, dsf, dbs)
    from dsfforza.build import clone_vehicle
    from dsfforza.formats.chnk import read_chunk

    with dsf.open("rb") as stream:
        taken = clone_vehicle.taken_ids(stream, read_chunk(stream, 0).entries)
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


WORK_FILES = ("chnk", "json", "job.json", "log", "fp.json", "first-try.json", "first-try.log", "first-try.fp.json")


class Reuse:
    """build --reuse DIR: a car whose earlier output in DIR/work was made from the same
    job, the same input files and the same code (src/dsfforza/build/fingerprint.py) is
    copied instead of converted. --rebuild-where builds the cars an expression over their
    earlier report picks again anyway; --trust-code leaves the code out of the comparison
    (the expression then names the cars a change affects). --verify-reuse N converts N
    of the reused cars again and stops the build if any comes out different.
    --reuse may be given more than once: each car is taken from the first build that
    has a matching copy of it (src/dsfforza/build/install.py passes the last finished
    build and an interrupted one)."""

    def __init__(self, args, work: Path):
        from dsfforza.build import fingerprint

        given = args.reuse if isinstance(args.reuse, list | tuple) else [args.reuse]
        self.sources = [Path(s) for s in given]
        for source in self.sources:
            if not (source / "work").is_dir():
                raise SystemExit(f"--reuse {source}: no work/ folder there")
            if (source / "work").resolve() == work.resolve():
                raise SystemExit("--reuse names this build's own --out (use --resume for that)")
        self.source = self.sources[0]
        self.old = self.source / "work"
        self.work = work
        self.where = getattr(args, "rebuild_where", None)
        self.trust_code = getattr(args, "trust_code", False)
        self.verify_count = getattr(args, "verify_reuse", 2)
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

    def decide(self, job: dict) -> tuple[dict | None, str]:
        """(the earlier report, "") when the car can be reused, else (None, why not)."""
        report, _source, why = self.decide_from(job)
        return report, why

    def decide_from(self, job: dict) -> tuple[dict | None, Path | None, str]:
        """(the earlier report, the build it is in, "") for the first --reuse build that
        has a matching copy of the car, else (None, None, why the first build's doesn't)."""
        first_why = None
        for source in self.sources:
            report, why = self._decide_in(source / "work", job)
            if report is not None:
                return report, source, ""
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
        names = ", ".join(str(s) for s in self.sources)
        print(f"--reuse {names}: {len(self.taken)} cars reused, {len(self.rebuilt)} to build")
        reasons: dict[str, list[str]] = {}
        for media, why in self.rebuilt.items():
            reasons.setdefault(why, []).append(media)
        for why, cars in sorted(reasons.items(), key=lambda kv: -len(kv[1])):
            print(f"  {why}: {len(cars)} ({', '.join(cars[:6])}{', ...' if len(cars) > 6 else ''})")

    def verify(self, jobs: list[dict], parallel: int, progress=None, on_mismatch: str = "stop") -> list[str]:
        """Convert a random few of the reused cars again into work/verify/ and require the
        same bytes. When any differs (or fails), on_mismatch "stop" raises SystemExit
        (before the merge: port_batch build, for developers); "rebuild" (dsff install, for
        players: issue #133) keeps going: a car that came out different takes its fresh
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
                f"repeatable (issue #84; research/batch.md, 'Selective rebuild'); build without --reuse, or with "
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
            "from": str(self.source),
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

    picks = select(args, cars, dsf, dbs)
    skipped = [p for p in picks if p.notes]
    picks = [p for p in picks if not p.notes]
    from dsfforza.build import clone_vehicle
    from dsfforza.formats.chnk import read_chunk

    with dsf.open("rb") as stream:
        vanilla_entries = read_chunk(stream, 0).entries
        taken = clone_vehicle.taken_ids(stream, vanilla_entries)
    assign_ids(picks, taken, args.id_policy, args.first_id, isos)
    root_limit = root_limit_of(args)
    room = (root_limit - len(vanilla_entries)) // 3  # 453 root entries in the vanilla archive
    if len(picks) > room:
        raise SystemExit(
            f"{len(picks)} cars: the archive root holds at most {room} new ones (--root-limit {root_limit})"
        )
    print_plan(picks + skipped)
    progress = getattr(args, "progress", None)
    emit(
        progress,
        "plan",
        cars=[p.media for p in picks],
        skipped=[{"media": p.media, "error": "; ".join(p.notes)} for p in skipped],
    )
    # Warm the shared caches once so workers only read them.
    from dsfforza.build import host_select

    host_select.survey(dsf)
    if port_args.handling == "dsf" and not port_args.no_handling:
        from dsfforza.build import handling_dsf

        handling_dsf.load_fleet(dsf)
    if not port_args.no_forza_sound:
        from dsfforza.convert import engine_sound

        engine_sound.vanilla_index(dsf)  # templates and reserved Wwise IDs (out/cache/sound/vanilla-banks.json)
    timings["plan"] = round(time.time() - started, 1)

    jobs = [
        {
            "media": p.media,
            "game": p.game,
            "year": p.year,
            "games": p.games,
            "source": p.source,
            "spool_id": p.spool_id,
            "dsf": str(dsf),
            "game_dir": str(game_from_dsf(dsf)),
            "cache": str(args.cache),
            "isos": [str(i) for i in isos],
            "port_options": port_options,
            "sound_dir": str(out_dir / "sound"),
            "chunk": str(work / f"{p.media}.chnk"),
            "report": str(work / f"{p.media}.json"),
        }
        for p in picks
    ]
    reused = {}
    reuse = Reuse(args, work) if getattr(args, "reuse", None) else None
    t = time.time()
    for job in jobs:  # results of an earlier run: reuse (--resume, --reuse) or drop
        old_job, old_report = work / f"{job['media']}.job.json", Path(job["report"])
        if args.resume and old_job.exists() and old_report.exists() and Path(job["chunk"]).exists():
            try:
                before = json.loads(old_job.read_text(encoding="utf-8"))
                report = json.loads(old_report.read_text(encoding="utf-8"))
            except ValueError:  # cut short by an earlier crash: build the car again
                before, report = {}, {}
            same = (
                all(before.get(k) == job[k] for k in ("spool_id", "game", "dsf"))
                and before.get("requested_options", before.get("port_options")) == job["port_options"]
            )
            if same and report.get("ok"):
                reused[job["media"]] = report
                continue
        # Everything an earlier run left for this car, so nothing stale is read as this run's.
        for name in WORK_FILES + ("tmp", "json.tmp", "fp.json.tmp"):
            (work / f"{job['media']}.{name}").unlink(missing_ok=True)
        if reuse is not None:
            report = reuse.take(job)
            if report is not None:
                reused[job["media"]] = report
    if reuse is not None:
        timings["reuse_check"] = round(time.time() - t, 1)
    for job in jobs:
        job["requested_options"] = list(job["port_options"])
    t = time.time()
    todo = [j for j in jobs if j["media"] not in reused]
    args.jobs = gentle.jobs(args.jobs)
    print(
        f"building {len(todo)} cars, {args.jobs} at a time ("
        + ("Idle priority on two CPUs: out/gentle" if gentle.enabled() else "Below Normal priority")
        + ")"
        + (
            f"; {len(reused)} reused"
            + (f" from {', '.join(map(str, reuse.sources))}" if reuse is not None else " from the last run")
            if reused
            else ""
        )
    )
    if reuse is not None:
        reuse.summary()
    emit(
        progress,
        "convert",
        todo=[j["media"] for j in todo],
        reused=sorted(reused),
        rebuilt=dict(reuse.rebuilt) if reuse is not None else {},
        jobs=args.jobs,
        gentle=gentle.enabled(),
    )
    results = {**reused, **run_workers(todo, work, args.jobs, progress=progress)}
    timings["convert"] = round(time.time() - t, 1)
    if reuse is not None:
        t = time.time()
        # A rebuilt car that differs from its reused output stops the build (--verify-mismatch
        # stop), or (rebuild, dsff install) its fresh copy is kept and its build's other cars
        # are converted again.
        redo = reuse.verify(jobs, args.jobs, progress=progress, on_mismatch=getattr(args, "verify_mismatch", "stop"))
        timings["verify_reuse"] = round(time.time() - t, 1)
        if redo:
            import shutil

            t = time.time()
            by_media = {j["media"]: j for j in jobs}
            for media in redo:
                for name in WORK_FILES + ("tmp", "json.tmp", "fp.json.tmp"):
                    (work / f"{media}.{name}").unlink(missing_ok=True)
                shutil.rmtree(Path(by_media[media]["sound_dir"]) / media, ignore_errors=True)
                reused.pop(media, None)
            results.update(run_workers([by_media[m] for m in redo], work, args.jobs, progress=progress))
            timings["convert_again"] = round(time.time() - t, 1)
        for media in reuse.mismatch.get("fresh_copies", []):
            fresh = Path(work / f"{media}.json")
            with contextlib.suppress(OSError, ValueError):
                results[media] = json.loads(fresh.read_text(encoding="utf-8"))
        args.reuse_record = reuse.record()

    ok = [results[p.media] for p in picks if results.get(p.media, {}).get("ok")]
    failed = [results[p.media] for p in picks if not results.get(p.media, {}).get("ok")]
    failed += [{"media": p.media, "ok": False, "cause": "not selected", "error": "; ".join(p.notes)} for p in skipped]
    pack = None
    if not getattr(args, "no_pack_reuse", False):
        from dsfforza.build import pack_reuse

        # This folder's own earlier pack (--resume, a rerun) and the --reuse builds'.
        settings = pack_reuse.settings_of(args, dsf, root_limit, gui_isos(args))
        pack = pack_reuse.PackReuse(
            out_dir, reuse.sources if reuse is not None else [], settings, game_dir=game_from_dsf(dsf)
        )
    try:
        finish(args, dsf, out_dir, work, ok, failed, port_args, port_options, timings, started, results, pack)
    finally:
        if pack is not None and pack.recorder is not None:
            pack.recorder.stop()


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
    from dsfforza.build import pack_reuse

    archive = out_dir / "dngvehicles.sp"
    progress = getattr(args, "progress", None)
    if not ok:
        write_report(out_dir, archive, [], failed, timings, started, args)
        raise SystemExit("no car was built")
    verify = copy.deepcopy((ok, failed)) if pack is not None and getattr(args, "verify_pack", False) else None
    reused = None
    if pack is not None:
        t = time.time()
        pack.prepare(ok, work)
        earlier = pack.whole()
        timings["pack_check"] = round(time.time() - t, 1)
        if earlier is not None:
            print(f"pack: the same cars, settings, inputs and code as {earlier.folder}'s pack; reusing it")
            emit(progress, "pack_reused", source=str(earlier.folder), cars=len(ok))
            t = time.time()
            reused = pack.take_whole(earlier)
            timings["pack_reuse"] = round(time.time() - t, 1)
        else:
            pack.start_recording()
    if reused is not None:
        cars_out, merge_failed = reused
        failed += merge_failed
        for f in merge_failed:
            emit(progress, "car_failed", media=f["media"], cause=f.get("cause"), error=f.get("error"))
    else:
        t = time.time()
        print(f"merging {len(ok)} cars into {archive}")
        emit(progress, "merge", cars=len(ok))
        cars_out, merge_failed = merge(dsf, archive, ok, work, root_limit=root_limit_of(args))
        failed += merge_failed
        timings["merge"] = round(time.time() - t, 1)
        for f in merge_failed:
            emit(progress, "car_failed", media=f["media"], cause=f.get("cause"), error=f.get("error"))
        if not cars_out:
            if pack is not None:
                pack.forget()
            write_report(out_dir, archive, [], failed, timings, started, args, results)
            raise SystemExit("no car could be merged")
        t = time.time()
        parallel = max(1, min(args.jobs, 4))
        if pack is None:
            emit(progress, "lint", cars=len(cars_out), total=len(cars_out))
            lint_all(archive, dsf, cars_out, parallel)
        else:

            def run(cars: list[dict], defined: set[int]) -> dict:
                print(f"lint: {len(cars)} of {len(cars_out)} cars")
                emit(progress, "lint", cars=len(cars), total=len(cars_out))
                return lint_all(
                    archive,
                    dsf,
                    cars,
                    parallel,
                    facts=True,
                    defined=defined,
                    records=pack.records,
                    record_dir=out_dir / pack_reuse.FOLDER,
                )

            pack.lint(cars_out, run)
        timings["lint"] = round(time.time() - t, 1)
    lint_failed = [c for c in cars_out if c.get("lint") != "ok"]
    if lint_failed:
        print(f"{len(lint_failed)} car(s) failed lint; they stay in the archive but are listed as failed")
    for c in lint_failed:
        emit(progress, "lint_failed", media=c["media"], text=c.get("lint"))

    from dsfforza.build import port_car

    manifest = {
        "archive": str(archive),
        "vanilla": str(dsf),
        "vanilla_sha256": sha256(dsf),
        "host": port_args.host,
        "sound_host": port_args.sound_host,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "textures": port_args.textures,
        "renewed_uids": not port_args.share_host_uids,
        "cockpit": not port_args.no_cockpit,
        "cars": cars_out,
        "probe_ids": [c["spool_id"] for c in cars_out],
        "batch": {
            "id_policy": args.id_policy,
            "port_options": port_options,
            "failed": [{k: f.get(k) for k in ("media", "spool_id", "cause", "error")} for f in failed],
        },
    }
    manifest_path = out_dir / "dngvehicles.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, default=str) + "\n", encoding="utf-8")
    (out_dir / "dngvehicles.lua").write_text(port_car.lua_snippet(manifest), encoding="utf-8", newline="\n")
    if args.gui and reused is None:
        t = time.time()
        from dsfforza.game import gui_assets

        emit(progress, "gui", cars=len(cars_out))

        def gui_log(text=""):
            print(text)
            emit(progress, "gui_log", text=str(text))

        try:
            gui_assets.build(
                manifest_path,
                out_dir / "gui",
                log=gui_log,
                archive=archive,
                wide_glyphs=getattr(args, "wide_glyphs", False),
                # The same CHNK reader limit: with dsfforza.asi [archive] gui.sp gets one
                # SPOI per picture too, not shared SPOIs past 177 (research/gui-assets.md).
                root_limit=getattr(args, "root_limit", None) or ENTRY_LIMIT,
                jobs=max(1, args.jobs),
                # The disc images by title (logos, names): any file names, FH1 not needed.
                isos=gui_isos(args),
                cache=pack.picture_cache() if pack is not None else None,
                record=pack.records if pack is not None else None,
            )
        except Exception as error:  # the archive is still usable without pictures and badges
            print(f"gui assets failed: {type(error).__name__}: {error}")
            timings["gui_error"] = f"{type(error).__name__}: {error}"
            emit(progress, "gui_failed", error=timings["gui_error"])
            # An earlier run's gui/ (or this run's half) doesn't match the new archive's IDs, and
            # install.ps1 installs whatever gui/ holds: move it aside, nothing is deleted.
            gui = out_dir / "gui"
            if gui.exists():
                stale = out_dir / f"gui-stale-{datetime.datetime.now():%Y%m%d-%H%M%S}"
                try:
                    gui.rename(stale)
                    print(f"moved {gui} to {stale.name}: it doesn't match this archive")
                    timings["gui_stale"] = str(stale)
                except OSError as move_error:
                    print(f"warning: {gui} doesn't match this archive and could not be moved: {move_error}")
        timings["gui"] = round(time.time() - t, 1)
    if pack is not None:
        t = time.time()
        pack.save(
            merge_failed,
            pack_outputs(out_dir, cars_out, args.gui) + pack.image_outputs(),
            timings.get("gui_error"),
        )
        timings["pack_record"] = round(time.time() - t, 1)
        timings["pack_reuse_stats"] = pack.stats
    if verify is not None:
        t = time.time()
        verify_pack(args, dsf, out_dir, work, verify[0], verify[1], port_args, port_options, results)
        timings["verify_pack"] = round(time.time() - t, 1)
    write_report(out_dir, archive, cars_out, failed, timings, started, args, results)
    emit(progress, "done", report=str(out_dir / "batch-report.json"))
    if getattr(args, "clean_work", False):
        for c in cars_out:
            (work / f"{c['media']}.chnk").unlink(missing_ok=True)


def gui_isos(args) -> list[Path]:
    return [Path(i) for i in (getattr(args, "iso", None) or forza_roster.default_images())]


def pack_outputs(out_dir: Path, cars: list[dict], gui: bool) -> list[str]:
    """The files a pack of `cars` wrote, relative to out_dir (dngvehicles.json and .lua are
    written every time): the archive, gui/ (not the previews of cars no longer in it)
    and pack/pictures/."""
    from dsfforza.build import pack_reuse

    names = {f"dsfforza_{c['media'].lower()}" for c in cars}
    out = ["dngvehicles.sp", "dngvehicles.json"]
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
    diffs = compare_packs(out_dir, folder, args.gui)
    if diffs:
        raise SystemExit(
            f"--verify-pack: the pack differs from a full pack of the same cars in {len(diffs)} file(s): "
            f"{'; '.join(diffs[:8])}. The full pack is in {folder}; build with --no-pack-reuse, and see "
            "research/batch.md, 'Pack reuse'."
        )
    print("--verify-pack: identical to a full pack")
    shutil.rmtree(folder, ignore_errors=True)


def compare_packs(mine: Path, full: Path, gui: bool) -> list[str]:
    """What differs between two packs of the same cars: the archive, the Lua, the manifest
    (but its time and archive path) and every file of the full pack's gui/."""
    from dsfforza.build import pack_reuse

    diffs = []

    def same(a: Path, b: Path) -> bool:
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
        expected = set(pack_outputs(full, json.loads((full / "dngvehicles.json").read_text())["cars"], gui))
        extra = [
            rel
            for rel in pack_outputs(mine, json.loads((mine / "dngvehicles.json").read_text())["cars"], gui)
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
    from dsfforza.build import clone_vehicle
    from dsfforza.formats.chnk import read_chunk

    with args.dsf.open("rb") as stream:
        taken = clone_vehicle.taken_ids(stream, read_chunk(stream, 0).entries)
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


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 22):
            digest.update(block)
    return digest.hexdigest().upper()


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


def build_parser() -> argparse.ArgumentParser:
    """The plan / build / remerge command line (src/dsfforza/build/install.py builds its
    port_batch arguments with it too)."""
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "build"):
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
            "through dsfforza.asi (research/asi.md)",
        )
        p.add_argument("--first-id", type=int, default=HIGH_FIRST_ID)
        p.add_argument(
            "--root-limit",
            type=int,
            default=ENTRY_LIMIT,
            help=f"root entries allowed (default {ENTRY_LIMIT}, the vanilla reader); "
            "more needs dsfforza.asi [archive], e.g. 3003 for every base ID below 1000",
        )
        if name == "plan":
            p.add_argument("--tsv", type=Path)
        else:
            p.add_argument("--out", type=Path, required=True, help="output folder")
            p.add_argument(
                "--jobs",
                type=int,
                default=max(1, min(6, (os.cpu_count() or 2) // 2)),
                help="worker processes (default: half the logical CPUs, at most 6)",
            )
            p.add_argument("--port", help=f"port_car options for every car (default: {DEFAULT_PORT_OPTIONS!r})")
            p.add_argument("--gui", action="store_true", help="also build gui.sp, Fonts.fchunk and hud.lua")
            p.add_argument(
                "--wide-glyphs",
                action="store_true",
                help="with --gui: every car its own HUD badge/plate as 16-bit glyphs "
                "(needs dsfforza.asi [hud]; gui_assets.py --wide-glyphs)",
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
                default=2,
                metavar="N",
                help="with --reuse: build N random reused cars again and stop if any differs (default 2; 0: off)",
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
        help=f"root entries allowed (default {ENTRY_LIMIT}; more needs dsfforza.asi [archive])",
    )
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--gui", action="store_true")
    r.add_argument("--wide-glyphs", action="store_true", help="with --gui: 16-bit HUD glyphs (dsfforza.asi [hud])")
    return parser


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    safe_stdio()  # python -m dsfforza.build.port_batch, and the _worker processes
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(line_buffering=True)
    if argv[:1] == ["_worker"]:
        sys.exit(worker(Path(argv[1])))
    args = build_parser().parse_args(argv)
    args.dsf = game_paths.vanilla(args.dsf)  # the original when a dsfforza install replaced it
    {"plan": cmd_plan, "build": cmd_build, "remerge": cmd_remerge}[args.cmd](args)


if __name__ == "__main__":
    main()
