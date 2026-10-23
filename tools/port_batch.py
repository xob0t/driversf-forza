"""Port many Forza cars into one dngvehicles.sp (batch mode for tools/port_car.py).

    python tools/port_batch.py plan  [selection]            # what would be built, with hosts
    python tools/port_batch.py build --out out/batch/b1 [selection] [--jobs 6] [--gui] \\
        [--port "--mdxn-limit 0x2C0000 --damage"]

Selection (combinable; a list keeps its order, filters keep roster order):

    --car NAME[@fh1|@fm4] ...   --list FILE (one car per line, # comments)
    --game fh1|fm4              only cars taken from that game (FH1 is preferred when on both)
    --make NIS,BMW,Ford         Forza make code or make name
    --body sedan,hatch,...      host_select body class: sedan coupe convertible hatch supercar
                                muscle suv pickup race (race cars are left out unless named)
    --year 1960-1979            --no-counterpart / --counterpart (a stock DSF car of the same model)
    --exclude NAME ...          --diverse N (round-robin over body classes, N cars)  --max N

Every car is built by port_car.build_car in its own worker process (Below
Normal priority, at most --jobs at a time, a new one only while 1.5 GB of RAM
is free), which writes the car's three VEHC entries to work/<media>.chnk and
a report. The merge then registers them into one copy of the stock archive
(SSLP/VSCT records), checks the root entry limit (--root-limit, default 1100:
150 stock cars plus at most 215 new ones; dsfforza.asi's archive module lifts
it, research/spool-limits.md) and UID clashes, writes the archive, and lints every car.
A car that fails in tools/damage.py is retried once without --damage, one
that fails in the cockpit step once with --no-cockpit (the host's interior);
a car that still fails is left out and listed with its cause (the innermost
tools module and exception), and the rest still ship. Extractions are cached
in out/cache/<game>/<media>/ (port_car), and --resume reuses every car an
earlier run in the same --out built with the same ID and options.

ID policy (research/batch.md): the garage lists model IDs 0..303 only
(research/gui-assets.md) and stock cars use 62..303, so the first cars of a
batch get IDs 1..61 (garage-visible), the rest 304 upwards (traffic, Shift and
scripts, but never the garage). --id-policy high puts every car at 304+
(the range confirmed in game); --first-id moves the high range.
--id-policy asi is for installs with dsfforza.asi (tools/asi, research/asi.md):
its garage module lists IDs up to 999 in up to 120 maker groups and keeps the
save flags of IDs 304+ in a side file, so every car can be in the garage. Cars
get every free ID in order: 1..61, the 92 IDs in 62..303 the stock archive
leaves unused, then --first-id up; cars that won't fit a maker group get a note.

Outputs in --out DIR: dngvehicles.sp, dngvehicles.json (port_car's manifest
format, plus "batch": failures and timings), dngvehicles.lua (the
dsfforza\\ported.lua for register.lua), batch-report.json (per car its
engine sound: Forza source banks, loops used, pitch ranges, SBAK size, or why
it kept the host's), sound/<media>/ (sweep_new.wav and sweep_template.wav
previews, wav/ loops, report.json; tools/engine_sound.py) and, with --gui,
gui/ (gui.sp, Fonts.fchunk, hud.lua from tools/gui_assets.py). A car whose
sound step crashes is retried once with --no-forza-sound. Every
port_car option is accepted inside --port "..." (e.g. --damage, --host 266,
--mdxn-limit 0x2C0000).

The game folder is read from --dsf (DIR/Install/dngvehicles.sp) and passed to
the other tools as DSF_GAME_DIR; derived data the tools need (vehiclestats.lua,
gamedb.slt) is made by tools/game_data.py prepare when missing.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import game_paths  # noqa: E402
import gentle  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
DEFAULT_GAME = Path("D:/Games/Driver San Francisco")
GARAGE_IDS = range(1, 62)          # garage loop covers 0..303; 0 is left alone, stock starts at 62
HIGH_FIRST_ID = 304
ROOT_ENTRY_LIMIT = 1100          # stock CHNK reader; more needs dsfforza.asi [archive] (--root-limit)
WORKER_TIMEOUT = 45 * 60
FREE_RAM_FOR_WORKER = 1536 << 20
BELOW_NORMAL = 0x00004000
DEFAULT_PORT_OPTIONS = "--mdxn-limit 0x2C0000 --damage"


# --- environment ------------------------------------------------------------------------

def game_from_dsf(dsf: Path) -> Path:
    game = dsf.resolve().parent.parent
    # <game>/dsfforza-backup/Install/dngvehicles.sp (tools/game_paths.py): the game is one up.
    if game == game_paths.REPO_STOCK.resolve():  # the repo's stock copy (a junction in a worktree): the real game
        return Path(os.environ.get("DSF_GAME_DIR") or DEFAULT_GAME)
    return game.parent if game.name == game_paths.BACKUP else game


def set_game_env(dsf: Path) -> None:
    """Point every tool at this game folder (host_select, gui_assets read DSF_GAME_DIR
    when imported, so call this before importing them)."""
    os.environ["DSF_GAME_DIR"] = str(game_from_dsf(dsf))


def free_ram() -> int | None:
    if os.name != "nt":
        return None

    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("page_total", ctypes.c_ulonglong),
                    ("page_avail", ctypes.c_ulonglong), ("virt_total", ctypes.c_ulonglong),
                    ("virt_avail", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]
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
    gentle.apply()  # out/gentle: Idle on two CPUs (tools/gentle.py)


def port_car_args(argv: list[str]) -> argparse.Namespace:
    """port_car.py's own options parsed from argv (so the batch takes all of them
    and their defaults), without running port_car."""
    import port_car

    class Parsed(Exception):
        pass
    real = argparse.ArgumentParser.parse_args

    def grab(self, args=None, namespace=None):
        raise Parsed(real(self, args, namespace))
    argparse.ArgumentParser.parse_args = grab
    try:
        port_car.main(argv)
    except Parsed as parsed:
        return parsed.args[0]
    finally:
        argparse.ArgumentParser.parse_args = real
    raise RuntimeError("port_car.main did not parse its arguments")


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
    counterpart: int | None = None       # stock car of the same model (identity 60)
    counterpart_model: str = ""
    spool_id: int | None = None
    notes: list[str] = field(default_factory=list)
    notes_soft: str = ""                 # informational (e.g. why no garage ID)


def load_roster(isos: list[Path]):
    from forza_roster import roster
    cars, _ = roster(isos)
    return {k: c for k, c in cars.items() if c.kind == "car"}


def gamedbs(isos: list[Path], cache: Path) -> dict[str, Path]:
    import port_car
    out = {}
    for game in ("FH1", "FM4"):
        db = port_car.extract_gamedb(isos, game, cache)
        if db:
            out[game] = db
    return out


def describe_cars(cars: dict, requests: list[tuple[str, str | None]], dsf: Path, dbs: dict[str, Path]) -> list[Pick]:
    """A Pick per (media, game or None) with body class, best host and the stock
    counterpart; unknown names become picks with a note."""
    import host_select as hs
    from port_car import CarRequest, pick_source
    hosts = hs.survey(dsf)
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
        info = hs.forza_info(rcar.name, dbs, source.game)
        ranking = hs.rank(info, hosts)
        best = ranking[0]
        counterpart = None
        for h in hosts:
            score, _ = hs.identity_score(h, info)
            if score >= 60 and not h["problems"]:
                counterpart = h
                break
        picks.append(Pick(rcar.name, source.game, rcar.year, rcar.games, asdict(source), make=info.make,
                          body=info.body, drive=info.drive, power_kw=info.engine.power_kw,
                          host=None if best.excluded else best.id, host_model=best.host["model"],
                          host_score=best.score, counterpart=counterpart["id"] if counterpart else None,
                          counterpart_model=counterpart["model"] if counterpart else "",
                          notes=[] if not best.excluded else [f"no usable host ({best.excluded})"]))
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
        requests = [(c.name, args.game.upper() if args.game else None) for c in
                    sorted(cars.values(), key=lambda c: c.name.lower())
                    if not args.game or args.game.upper() in c.games.split(",")]
    picks = describe_cars(cars, requests, dsf, dbs)
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
        if not explicit:   # filters apply to the roster; a named list is taken as given
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
        out = out[:args.max]
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
# 40 groups (stock: 35 selectable) and 20 cars per group (stock: Chevrolet 16), with no
# capacity check, so a garage ID is only given while its maker group has room. The
# margin leaves space for mods that make more stock cars selectable (Felony Rework);
# register.lua checks the real table again at runtime.
GARAGE_GROUP_CAP = 20 - 1
GARAGE_GROUPS_CAP = 40 - 1
ASI_GROUPS_CAP = 120 - 1   # dsfforza.asi [garage] max_groups (default 120)
# --id-policy asi: the low IDs first. 62..303 holds 150 stock cars (and spool IDs without
# a vehicleStats entry: 123, 131, 277, 289); the 92 others appear in no traffic set,
# vehicleStats key or model-ID field of the game's Lua (research/asi.md), and the
# archive's own IDs are skipped through `taken` anyway.
ASI_LOW_IDS = list(range(1, 304))
RESERVED_IDS = {622, 623}   # Felony Rework's vehicleStats dummies (not models)


def garage_groups() -> tuple[dict[str, int], dict[str, str]]:
    """({group key: stock selectable cars}, {DSF maker key: group key}) from the stock
    vehicleStats. The group key is the ManufacturerName string ("ID:242753" = Chevrolet),
    the maker key what gui_assets.dsf_make returns ("CHEVROLET")."""
    try:
        import gui_assets
        stats = gui_assets.stock_stats()
        makes, _ = gui_assets.stock_codes(gui_assets.read_strings())
    except (OSError, ValueError, KeyError):
        return {}, {}
    counts: dict[str, int] = {}
    for sid, fields in stats.items():
        if fields.get("Selectable") == "true" and sid <= 303:
            key = fields.get("ManufacturerName", "")
            counts[key] = counts.get(key, 0) + 1
    return counts, {make: name for make, (_, name) in makes.items()}


def car_groups(picks: list["Pick"], group_of: dict[str, str]) -> dict[str, str]:
    """{media: garage group key} as gui_assets.build will name the maker: Forza's own
    maker name (tools/forza_strings.py via gui_assets.car_texts) mapped by
    gui_assets.dsf_make to a stock maker's "ID:nnnnnn" string, else the plain name."""
    import gui_assets
    texts = gui_assets.car_texts([{"media": p.media, "game": p.game} for p in picks])
    out = {}
    for p in picks:
        t = texts[p.media]
        key = gui_assets.dsf_make(t["make"], t["model"])
        out[p.media] = group_of.get(key, key)
    return out


OVERFLOW_GROUP = "FORZA"   # register.lua: makes without a garage group of their own share these


def garage_group(make: str, group: str, counts: dict[str, int], cap: int, groups_cap: int) -> str | None:
    """The garage maker group a car joins, or None: its own group while it has room (a new
    make opens one while fewer than groups_cap exist), else a shared FORZA, FORZA 2 ...
    group (the same rule as register.lua's garageGroup). One group slot is kept for FORZA."""
    if group in counts:
        if counts[group] < cap:
            return group
    elif len(counts) + (0 if OVERFLOW_GROUP in counts else 1) < groups_cap:   # keep room for FORZA
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
    """--root-limit: 1100 (stock) unless the install uses dsfforza.asi's archive module."""
    limit = getattr(args, "root_limit", None) or ROOT_ENTRY_LIMIT
    if limit > ROOT_ENTRY_LIMIT:
        print(f"note: root limit {limit}: the archive needs dsfforza.asi with [archive] enabled "
              f"(the stock game reads {ROOT_ENTRY_LIMIT} entries)", file=sys.stderr)
    return limit


def assign_ids(picks: list[Pick], taken: set[int], policy: str, first_high: int) -> None:
    """policy "garage": IDs 1..61 (listed in the garage) while a maker group has room
    for the car, else first_high and up; "high": first_high and up only; "asi"
    (installs with dsfforza.asi): every free ID below 1000 in order, 1..61, the 92
    IDs in 62..303 the stock archive doesn't use, then first_high up, each car
    garage-listed by the plugin while its maker group (ASI_GROUPS_CAP groups) has
    room. List order decides which cars get the garage IDs (garage) or the group
    slots (asi). IDs some mod is known to use (RESERVED_IDS) are skipped."""
    low = [i for i in GARAGE_IDS if i not in taken] if policy == "garage" else []
    asi = [i for i in ASI_LOW_IDS if i not in taken and i not in RESERVED_IDS] if policy == "asi" else []
    counts, group_of = garage_groups() if (low or policy == "asi") else ({}, {})
    groups = car_groups(picks, group_of) if (low or policy == "asi") else {}
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
            group = garage_group(own, own, counts, GARAGE_GROUP_CAP, ASI_GROUPS_CAP)
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
    """A short failure cause: a known message, else "<tools module>: <exception>"
    from the innermost frame in tools/ of the traceback."""
    import re
    for needle, cause in CAUSES:
        if needle in text:
            return cause
    frames = re.findall(r'tools[\\/](\w+)\.py", line \d+', text)
    frames = [f for f in frames if f not in ("vehicle_mesh", "chnk")] or frames   # the step, not a helper
    error = re.findall(r"^(\w+(?:Error|Exception|Exit)):", text, re.M)
    if frames:
        return f"{frames[-1]}.py: {error[-1] if error else 'error'}"
    return "conversion error"


def fallback_for(report: dict, options: list[str]) -> tuple[list[str], str] | None:
    """Options for one retry after a failure that a lesser build avoids: without
    --damage when tools/damage.py failed, with --no-cockpit (the host's interior)
    when tools/cockpit.py or the cockpit carbin failed. None: no retry."""
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
        return out, "without --damage (tools/damage.py failed)"
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
    started = time.time()
    lines: list[str] = []

    def log(text=""):
        print(text, flush=True)
        lines.append(str(text))

    report = {"media": job["media"], "game": job["game"], "spool_id": job["spool_id"], "ok": False}
    try:
        import clone_vehicle
        import forza2dsf
        import port_car
        from chnk import read_chunk, write_chunk
        from forza_roster import Source
        dsf = Path(job["dsf"])
        args = port_car_args(["--dsf", str(dsf), "--out", str(out_chunk.with_suffix(".unused.sp")),
                              "--car", f"{job['media']}@{job['game'].lower()}", "--cache", job["cache"]]
                             + job["port_options"])
        source = Source(**job["source"])
        isos = [Path(p) for p in job["isos"]]
        folder = port_car.extract_car(source, job["media"], args.cache, log)
        car = forza2dsf.load_car(folder, job["media"])
        gamedb = None
        try:
            dims = forza2dsf.car_dims(car)
        except ValueError:
            gamedb = port_car.extract_gamedb(isos, source.game, args.cache)
            dims = forza2dsf.car_dims(car, gamedb)
        if (not args.no_cockpit or not args.no_handling) and gamedb is None:
            gamedb = port_car.extract_gamedb(isos, source.game, args.cache)
        host, sound, choice = port_car.pick_hosts(args, job["media"], source.game, folder, gamedb, isos, log)
        with dsf.open("rb") as stream:
            root = read_chunk(stream, 0)
            entries = root.entries
            nonr_limit, mdxn_max = port_car.stock_limits(stream, entries)
            if args.mdxn_limit:
                mdxn_max = {kind: max(size, args.mdxn_limit) for kind, size in mdxn_max.items()}
            names = {s: entries[i].name for s, i, k in clone_vehicle.sslp_records(stream, entries) if k == 0}
            log(f"--- {job['media']} ({source.game}, {source.where}) -> spool ID {job['spool_id']}, host {host} "
                f"({names[host]})")
            if job.get("sound_dir"):
                args.forza_sound_dir = Path(job["sound_dir"])
            plan, plan_report = port_car.forza_sound_plan(
                args, stream, entries, job["media"], source.game, folder, gamedb, isos,
                args.sound_host if isinstance(args.sound_host, int) else None, job["spool_id"], log)
            clones, built = port_car.build_car(stream, entries, host, job["spool_id"], car, dims, args,
                                               nonr_limit, mdxn_max, log, gamedb, sound, plan, plan_report)
            built["host_choice"] = choice
            temp = out_chunk.with_suffix(".tmp")
            with temp.open("wb") as out:
                write_chunk(out, stream, clones, root.version)
            os.replace(temp, out_chunk)
        report.update({
            "ok": True, "host": host, "host_name": names[host], "sound": sound,
            "entry": {"media": job["media"], "name": port_car.display_name(job["media"], job["year"]),
                      "year": job["year"], "game": source.game, "games_available": job["games"],
                      "source": {"iso": Path(source.iso).name, "where": source.where, "package": source.package,
                                 "path": source.path},
                      "cache": str(Path(args.cache) / source.game.lower() / job["media"]),
                      "spool_id": job["spool_id"], "interior_id": job["spool_id"] + 1000,
                      "exterior_id": job["spool_id"] + 2000, "host": host, "host_name": names[host], **built,
                      **({"batch_fallback": job["fallback"]} if job.get("fallback") else {})},
            "port_args": {k: (str(v) if isinstance(v, Path) else v) for k, v in vars(args).items()}})
    except BaseException as error:  # SystemExit from port_car included
        text = f"{type(error).__name__}: {error}"
        report.update({"error": text, "cause": cause_of(text + "\n" + traceback.format_exc()),
                       "traceback": traceback.format_exc()[-4000:]})
        log(text)
    report["seconds"] = round(time.time() - started, 1)
    out_report.write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


# --- the pool ---------------------------------------------------------------------------------

def run_workers(jobs: list[dict], work: Path, parallel: int, log=print) -> dict[str, dict]:
    """Run one worker process per job, at most `parallel` at once, Below Normal priority."""
    pending = list(jobs)
    jobs_by_media = {j["media"]: j for j in jobs}
    running: dict[str, tuple[subprocess.Popen, float, object]] = {}
    results: dict[str, dict] = {}
    flags = gentle.creation_flags(BELOW_NORMAL if os.name == "nt" else 0)
    while pending or running:
        while pending and len(running) < parallel:
            ram = free_ram()
            if running and ram is not None and ram < FREE_RAM_FOR_WORKER:
                break
            job = pending.pop(0)
            job_path = work / f"{job['media']}.job.json"
            job_path.write_text(json.dumps(job, indent=1), encoding="utf-8")
            log_file = (work / f"{job['media']}.log").open("w", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(TOOLS / "port_batch.py"), "_worker", str(job_path)],
                                    stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags,
                                    cwd=str(REPO), env={**os.environ, "PYTHONUNBUFFERED": "1"})
            running[job["media"]] = (proc, time.time(), log_file)
            log(f"  start {job['media']} (ID {job['spool_id']}); {len(running)} running, {len(pending)} queued")
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
            report_path = work / f"{media}.json"
            if report_path.exists():
                report = json.loads(report_path.read_text(encoding="utf-8"))
            else:
                tail = (work / f"{media}.log").read_text(encoding="utf-8", errors="replace")[-1500:]
                report = {"media": media, "ok": False, "cause": "timeout" if code is None else "worker crash",
                          "error": f"exit code {code}; log tail: {tail}"}
            report["seconds"] = round(time.time() - started, 1) + jobs_by_media[media].get("seconds_before", 0)
            job = jobs_by_media[media]
            retry = None if report.get("ok") or job.get("fallback") else fallback_for(report, job["port_options"])
            if retry:
                options, why = retry
                (work / f"{media}.json").replace(work / f"{media}.first-try.json")
                (work / f"{media}.log").replace(work / f"{media}.first-try.log")
                job = {**job, "port_options": options, "fallback": why, "first_error": report.get("error"),
                       "first_cause": report.get("cause"), "seconds_before": report["seconds"]}
                jobs_by_media[media] = job
                pending.append(job)
                log(f"  retry {media} {why}")
                continue
            if job.get("fallback"):
                report.update({"fallback": job["fallback"], "first_error": job.get("first_error"),
                               "first_cause": job.get("first_cause")})
            results[media] = report
            state = "ok" if report.get("ok") else f"FAILED ({report.get('cause')}: {report.get('error', '')[:160]})"
            log(f"  done  {media} in {report['seconds']:.0f} s: {state}"
                + (f" ({job['fallback']})" if job.get("fallback") else ""))
    return results


# --- merge ----------------------------------------------------------------------------------

class MultiStream:
    """Several files read as one address space: the stock archive at 0 and each
    car's chunk file after it, so chnk.write_chunk can copy from all of them."""

    def __init__(self, first: Path):
        self.parts: list[tuple[int, int, object]] = []
        self.size = 0
        self.pos = 0
        self.add(first)

    def add(self, path: Path) -> int:
        handle = path.open("rb")
        size = path.stat().st_size
        base = self.size
        self.parts.append((base, base + size, handle))
        self.size += -(-size // 0x1000) * 0x1000
        return base

    def seek(self, pos: int, whence: int = 0) -> int:
        self.pos = pos if whence == 0 else self.pos + pos if whence == 1 else self.size + pos
        return self.pos

    def tell(self) -> int:
        return self.pos

    def read(self, n: int = -1) -> bytes:
        for start, end, handle in self.parts:
            if start <= self.pos < end:
                if n < 0:
                    n = end - self.pos
                handle.seek(self.pos - start)
                data = handle.read(min(n, end - self.pos))
                self.pos += len(data)
                return data
        return b""

    def close(self) -> None:
        for _, _, handle in self.parts:
            handle.close()


def merge(dsf: Path, out: Path, results: list[dict], work: Path, log=print,
          root_limit: int = ROOT_ENTRY_LIMIT) -> tuple[list[dict], list[dict]]:
    """Register every built car into a copy of the stock archive. Returns (entries
    of the manifest's "cars", failures found here)."""
    import clone_vehicle
    import port_car
    from chnk import read_chunk, write_chunk
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
            clones = read_chunk(stream, base).entries
            if len(clones) != 3 or any(c.tag != "VEHC" for c in clones):
                failed.append({**r, "ok": False, "cause": "merge", "error": "car chunk is not three VEHC entries"})
                continue
            new_id = r.get("new_id", r["spool_id"])
            if new_id != r["spool_id"]:
                # The ID lives only in VSVH, VO3D, SPID and UPVH of the full entry (clone_vehicle).
                clone_vehicle.patch_ids(stream, clones[0], r["spool_id"], new_id)
                r = {**r, "spool_id": new_id, "entry": {**r["entry"], "spool_id": new_id,
                                                         "interior_id": new_id + 1000,
                                                         "exterior_id": new_id + 2000}}
            if len(entries) + 3 > root_limit:
                failed.append({**r, "ok": False, "cause": "root entry limit",
                               "error": f"{len(entries)} root entries; the limit is {root_limit} (--root-limit)"})
                continue
            clone_vehicle.register_clones(stream, entries, clones, r["host"], r["spool_id"])
            cars.append(r["entry"])
        records = {s: i for s, i, _ in clone_vehicle.sslp_records(stream, entries)}
        for c in cars:
            c["vehc"] = [records[c["spool_id"] + k] for k in clone_vehicle.KIND_OFFSETS]
        clashes = port_car.uid_clashes(stream, entries, {i for c in cars for i in c["vehc"]})
        if clashes:
            bad = {entries[i].name for i in clashes}
            log(f"UID clashes in {sorted(bad)}; rebuild those cars")
            raise SystemExit(f"resource UID clashes: {sorted(bad)}")
        out.parent.mkdir(parents=True, exist_ok=True)
        temp = out.with_name(out.name + ".tmp")
        with temp.open("wb") as handle:
            write_chunk(handle, stream, entries, root.version)
        os.replace(temp, out)
        size = out.stat().st_size
        if size >= 1 << 31:
            # The stock file layer clamps seek positions of 2 GiB and up to 0 (spool-limits.md).
            log(f"note: {out.name} is {size / 2**30:.2f} GiB; past 2 GiB it needs dsfforza.asi "
                f"[archive] large_files=1 (the default)")
    finally:
        stream.close()
    return cars, failed


def lint_all(out: Path, dsf: Path, cars: list[dict], parallel: int, log=print) -> None:
    flags = gentle.creation_flags(BELOW_NORMAL if os.name == "nt" else 0)
    pending, running = list(cars), []
    while pending or running:
        while pending and len(running) < parallel:
            c = pending.pop(0)
            proc = subprocess.Popen([sys.executable, str(TOOLS / "upvh.py"), "lint", str(out), "--id",
                                     str(c["spool_id"]), "--stock", str(dsf), "--source", str(c["host"])],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    creationflags=flags)
            running.append((c, proc))
        c, proc = running.pop(0)
        text, _ = proc.communicate()
        c["lint"] = "ok" if proc.returncode == 0 else text.strip()
        log(f"  lint {c['spool_id']} {c['media']}: {'ok' if proc.returncode == 0 else 'FAILED'}")


# --- commands ---------------------------------------------------------------------------------

def common(args) -> tuple[Path, list[Path], dict, dict[str, Path]]:
    set_game_env(args.dsf)
    lower_priority()
    isos = [p.resolve() for p in (args.iso or sorted((REPO / "assets").glob("*.iso")))]
    if not isos:
        raise SystemExit("no Forza disc images (assets/*.iso or --iso)")
    import game_data
    game_data.prepare(game_from_dsf(args.dsf), isos, log=lambda t: print(f"prepare: {t}"))
    cars = load_roster(isos)
    dbs = gamedbs(isos, args.cache)
    return args.dsf, isos, cars, dbs


def print_plan(picks: list[Pick]) -> None:
    print(f"{'ID':>4}  {'car':30} {'game':4} {'year':4}  {'body':11} {'drive':5} {'kW':>5}  host / counterpart")
    for p in picks:
        cp = f"   stock {p.counterpart} {p.counterpart_model}" if p.counterpart else ""
        host = f"{p.host} {p.host_model[:26]} ({p.host_score:.0f})" if p.host else "-"
        print(f"{p.spool_id or '':>4}  {p.media:30} {p.game:4} {p.year or '':4}  {p.body:11} {p.drive or '':5} "
              f"{p.power_kw or 0:5.0f}  {host}{cp}" + (f"  !! {'; '.join(p.notes)}" if p.notes else "")
              + (f"  ({p.notes_soft})" if p.notes_soft else ""))


def cmd_plan(args) -> None:
    dsf, isos, cars, dbs = common(args)
    picks = select(args, cars, dsf, dbs)
    from chnk import read_chunk
    import clone_vehicle
    with dsf.open("rb") as stream:
        taken = clone_vehicle.taken_ids(stream, read_chunk(stream, 0).entries)
    assign_ids([p for p in picks if not p.notes], taken, args.id_policy, args.first_id)
    print_plan(picks)
    print(f"{len(picks)} cars")
    if args.tsv:
        cols = ["spool_id", "media", "game", "year", "make", "body", "drive", "power_kw", "host", "host_model",
                "host_score", "counterpart", "counterpart_model", "notes"]
        rows = ["\t".join(cols)] + ["\t".join(str(getattr(p, c) if c != "notes" else "; ".join(p.notes))
                                              .replace("None", "") for c in cols) for p in picks]
        args.tsv.write_text("\n".join(rows) + "\n", encoding="utf-8")
        print(f"wrote {args.tsv}")


def cmd_build(args) -> None:
    started = time.time()
    timings = {}
    dsf, isos, cars, dbs = common(args)
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / "dngvehicles.sp"
    if archive.resolve() == dsf.resolve():
        raise SystemExit("refusing to overwrite the stock archive")
    work = out_dir / "work"
    work.mkdir(exist_ok=True)
    port_options = shlex.split(args.port if args.port is not None else DEFAULT_PORT_OPTIONS, posix=True)
    port_args = port_car_args(["--dsf", str(dsf), "--out", str(archive), "--car", "X"] + port_options)

    picks = select(args, cars, dsf, dbs)
    skipped = [p for p in picks if p.notes]
    picks = [p for p in picks if not p.notes]
    from chnk import read_chunk
    import clone_vehicle
    with dsf.open("rb") as stream:
        taken = clone_vehicle.taken_ids(stream, read_chunk(stream, 0).entries)
    assign_ids(picks, taken, args.id_policy, args.first_id)
    root_limit = root_limit_of(args)
    room = (root_limit - 453) // 3
    if len(picks) > room:
        raise SystemExit(f"{len(picks)} cars: the archive root holds at most {room} new ones "
                         f"(--root-limit {root_limit})")
    print_plan(picks + skipped)
    # Warm the shared caches once so workers only read them.
    import host_select
    host_select.survey(dsf)
    if port_args.handling == "dsf" and not port_args.no_handling:
        import handling_dsf
        handling_dsf.load_fleet(dsf)
    if not port_args.no_forza_sound:
        import engine_sound
        engine_sound.stock_index(dsf)    # templates and reserved Wwise IDs (out/cache/sound/stock-banks.json)
    timings["plan"] = round(time.time() - started, 1)

    jobs = [{"media": p.media, "game": p.game, "year": p.year, "games": p.games, "source": p.source,
             "spool_id": p.spool_id, "dsf": str(dsf), "game_dir": str(game_from_dsf(dsf)),
             "cache": str(args.cache), "isos": [str(i) for i in isos], "port_options": port_options,
             "sound_dir": str(out_dir / "sound"),
             "chunk": str(work / f"{p.media}.chnk"), "report": str(work / f"{p.media}.json")} for p in picks]
    reused = {}
    for job in jobs:   # results of an earlier run: reuse (--resume) or drop
        old_job, old_report = work / f"{job['media']}.job.json", Path(job["report"])
        if args.resume and old_job.exists() and old_report.exists() and Path(job["chunk"]).exists():
            before = json.loads(old_job.read_text(encoding="utf-8"))
            report = json.loads(old_report.read_text(encoding="utf-8"))
            same = all(before.get(k) == job[k] for k in ("spool_id", "game", "dsf")) and \
                before.get("requested_options", before.get("port_options")) == job["port_options"]
            if same and report.get("ok"):
                reused[job["media"]] = report
                continue
        for path in (Path(job["chunk"]), Path(job["report"])):
            path.unlink(missing_ok=True)
    for job in jobs:
        job["requested_options"] = list(job["port_options"])
    t = time.time()
    todo = [j for j in jobs if j["media"] not in reused]
    args.jobs = gentle.jobs(args.jobs)
    print(f"building {len(todo)} cars, {args.jobs} at a time ("
          + ("Idle priority on two CPUs: out/gentle" if gentle.enabled() else "Below Normal priority") + ")"
          + (f"; {len(reused)} reused from the last run" if reused else ""))
    results = {**reused, **run_workers(todo, work, args.jobs)}
    timings["convert"] = round(time.time() - t, 1)

    ok = [results[p.media] for p in picks if results.get(p.media, {}).get("ok")]
    failed = [results[p.media] for p in picks if not results.get(p.media, {}).get("ok")]
    failed += [{"media": p.media, "ok": False, "cause": "not selected", "error": "; ".join(p.notes)} for p in skipped]
    finish(args, dsf, out_dir, work, ok, failed, port_args, port_options, timings, started, results)


def finish(args, dsf: Path, out_dir: Path, work: Path, ok: list[dict], failed: list[dict], port_args,
           port_options: list[str], timings: dict, started: float, results: dict) -> None:
    """Merge, lint, manifest, Lua, GUI assets and the report."""
    archive = out_dir / "dngvehicles.sp"
    if not ok:
        write_report(out_dir, archive, [], failed, timings, started, args)
        raise SystemExit("no car was built")
    t = time.time()
    print(f"merging {len(ok)} cars into {archive}")
    cars_out, merge_failed = merge(dsf, archive, ok, work, root_limit=root_limit_of(args))
    failed += merge_failed
    timings["merge"] = round(time.time() - t, 1)
    t = time.time()
    lint_all(archive, dsf, cars_out, max(1, min(args.jobs, 4)))
    timings["lint"] = round(time.time() - t, 1)
    lint_failed = [c for c in cars_out if c.get("lint") != "ok"]
    if lint_failed:
        print(f"{len(lint_failed)} car(s) failed lint; they stay in the archive but are listed as failed")

    import port_car
    manifest = {"archive": str(archive), "stock": str(dsf), "stock_sha256": sha256(dsf), "host": port_args.host,
                "sound_host": port_args.sound_host,
                "created": datetime.datetime.now().isoformat(timespec="seconds"),
                "textures": port_args.textures, "renewed_uids": not port_args.share_host_uids,
                "cockpit": not port_args.no_cockpit, "cars": cars_out,
                "probe_ids": [c["spool_id"] for c in cars_out],
                "batch": {"id_policy": args.id_policy, "port_options": port_options,
                          "failed": [{k: f.get(k) for k in ("media", "spool_id", "cause", "error")} for f in failed]}}
    manifest_path = out_dir / "dngvehicles.json"
    manifest_path.write_text(json.dumps(manifest, indent=1, default=str) + "\n", encoding="utf-8")
    (out_dir / "dngvehicles.lua").write_text(port_car.lua_snippet(manifest), encoding="utf-8", newline="\n")
    if args.gui:
        t = time.time()
        import gui_assets
        try:
            gui_assets.build(manifest_path, out_dir / "gui", archive=archive,
                             wide_glyphs=getattr(args, "wide_glyphs", False))
        except Exception as error:   # the archive is still usable without pictures and badges
            print(f"gui assets failed: {type(error).__name__}: {error}")
            timings["gui_error"] = f"{type(error).__name__}: {error}"
        timings["gui"] = round(time.time() - t, 1)
    write_report(out_dir, archive, cars_out, failed, timings, started, args, results)
    if getattr(args, "clean_work", False):
        for c in cars_out:
            (work / f"{c['media']}.chnk").unlink(missing_ok=True)


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
    from chnk import read_chunk
    import clone_vehicle
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
    failed = [f for f in old.get("batch", {}).get("failed", [])]
    finish(args, args.dsf, args.out, work, ok, failed, port_args, port_options, {}, started,
           {r["media"]: r for r in ok})


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 22):
            digest.update(block)
    return digest.hexdigest().upper()


def write_report(out_dir: Path, archive: Path, cars: list[dict], failed: list[dict], timings: dict,
                 started: float, args, results: dict | None = None) -> None:
    import engine_sound
    timings["total"] = round(time.time() - started, 1)
    causes: dict[str, list[str]] = {}
    for f in failed:
        causes.setdefault(f.get("cause") or "?", []).append(f["media"])
    sounds = {c["media"]: engine_sound.summary(c.get("forza_sound") or {}) for c in cars}
    report = {
        "archive": str(archive), "archive_bytes": archive.stat().st_size if archive.exists() else None,
        "jobs": args.jobs, "timings_s": timings,
        "ok": [{"spool_id": c["spool_id"], "media": c["media"], "game": c["game"], "host": c["host"],
                "host_name": c["host_name"], "lint": c.get("lint"), "fallback": c.get("batch_fallback"),
                "seconds": (results or {}).get(c["media"], {}).get("seconds"),
                "lods": c.get("lods"), "sizes": c.get("sizes"), "sound": sounds[c["media"]]} for c in cars],
        "failed": [{k: f.get(k) for k in ("media", "spool_id", "cause", "error")} for f in failed],
        "failed_by_cause": causes,
        "forza_sound": {"cars": sum(s["forza"] for s in sounds.values()),
                        "host_sound": {m: s["reason"] for m, s in sounds.items() if not s["forza"]},
                        "previews": str(out_dir / "sound")},
        "fallbacks": {c["media"]: {"fallback": c.get("batch_fallback"),
                                   "first_cause": (results or {}).get(c["media"], {}).get("first_cause")}
                      for c in cars if c.get("batch_fallback")},
    }
    (out_dir / "batch-report.json").write_text(json.dumps(report, indent=1, default=str) + "\n", encoding="utf-8")
    print(f"\n{len(cars)} cars built, {len(failed)} failed; total {timings['total']:.0f} s "
          f"({', '.join(f'{k} {v}' for k, v in timings.items() if k != 'total')})")
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


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    if argv[:1] == ["_worker"]:
        sys.exit(worker(Path(argv[1])))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plan", "build"):
        p = sub.add_parser(name)
        p.add_argument("--dsf", type=Path, default=DEFAULT_GAME / "Install" / "dngvehicles.sp",
                       help="stock Install/dngvehicles.sp (only read); its game folder is used for the rest")
        p.add_argument("--iso", type=Path, action="append", help="Forza disc images (default: assets/*.iso)")
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
        p.add_argument("--id-policy", choices=("garage", "high", "asi"), default="garage",
                       help="garage (default): IDs 1..61 first, then --first-id up; high: --first-id up only; "
                            "asi: every free ID from 1 (stock gaps in 62..303 too), then --first-id up, garage-listed "
                            "through dsfforza.asi (research/asi.md)")
        p.add_argument("--first-id", type=int, default=HIGH_FIRST_ID)
        p.add_argument("--root-limit", type=int, default=ROOT_ENTRY_LIMIT,
                       help=f"root entries allowed (default {ROOT_ENTRY_LIMIT}, the stock reader); "
                            "more needs dsfforza.asi [archive], e.g. 3003 for every base ID below 1000")
        if name == "plan":
            p.add_argument("--tsv", type=Path)
        else:
            p.add_argument("--out", type=Path, required=True, help="output folder")
            p.add_argument("--jobs", type=int, default=max(1, min(6, (os.cpu_count() or 2) // 2)),
                           help="worker processes (default: half the logical CPUs, at most 6)")
            p.add_argument("--port", help=f"port_car options for every car (default: {DEFAULT_PORT_OPTIONS!r})")
            p.add_argument("--gui", action="store_true", help="also build gui.sp, Fonts.fchunk and hud.lua")
            p.add_argument("--wide-glyphs", action="store_true",
                           help="with --gui: every car its own HUD badge/plate as 16-bit glyphs "
                                "(needs dsfforza.asi [hud]; gui_assets.py --wide-glyphs)")
            p.add_argument("--resume", action="store_true",
                           help="reuse cars an earlier run in --out built with the same ID and options")
            p.add_argument("--clean-work", action="store_true",
                           help="delete the per-car chunk files afterwards (default: keep them for remerge)")
    r = sub.add_parser("remerge", help="re-assemble an earlier build under another ID policy (no conversion)")
    r.add_argument("source", type=Path, help="an earlier build's --out folder (with work/)")
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--dsf", type=Path, default=DEFAULT_GAME / "Install" / "dngvehicles.sp")
    r.add_argument("--id-policy", choices=("garage", "high", "asi"), default="high")
    r.add_argument("--first-id", type=int, default=HIGH_FIRST_ID)
    r.add_argument("--root-limit", type=int, default=ROOT_ENTRY_LIMIT,
                   help=f"root entries allowed (default {ROOT_ENTRY_LIMIT}; more needs dsfforza.asi [archive])")
    r.add_argument("--jobs", type=int, default=4)
    r.add_argument("--gui", action="store_true")
    r.add_argument("--wide-glyphs", action="store_true", help="with --gui: 16-bit HUD glyphs (dsfforza.asi [hud])")
    args = parser.parse_args(argv)
    args.dsf = game_paths.stock(args.dsf)  # the original when a dsfforza install replaced it
    {"plan": cmd_plan, "build": cmd_build, "remerge": cmd_remerge}[args.cmd](args)


if __name__ == "__main__":
    main()
