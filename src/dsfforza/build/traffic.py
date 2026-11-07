"""The car manager's traffic settings: ambient traffic sets (a patched copy of the game's
media/san_franciscoAI.fchunk) and interesting vehicles (dsfcars\\traffic.lua for register.lua).
research/traffic.md has the game's side; section 6 the design.

Settings (manager.json "traffic"; keys are package keys, so they survive reinstalls):

    {"cars": {"<key>": {"traffic": false, "regions": [6, 9]}},
     "ambient": {"default": {...set settings...}, "sets": {"Chapter 7": {...}}, "seed": 0},
     "iv": {"ports": true, "per_region": 24}}

Set settings (ambient.default for every story set, ambient.sets.<name> over it for one):

    ports        "fill" (default: up to the set's slot budget), how many ports join the set (dealt
                 from the cars in traffic), or a list of keys
    share        "mean" (default: each added model as likely on a road as the road's average
                 vanilla model, so the added models' share is k / (vanilla + k)), or their share of
                 every road's weight (0..1)
    weights      "vanilla", "equal" or "custom"
    blend        0..1 between vanilla (0) and equal (1); "equal" is 1, "vanilla" 0
    custom       {model ID or key: factor}: a model's weight times this (weights "custom")
    vanilla_off  vanilla model IDs taken out of the set
    extra        vanilla model IDs from other sets added in (like ports, at the share)

With no settings (a fresh install) every story set gets DEFAULT_SET: filled to its budget,
"mean" weights; `traffic ambient --off` stores {"off": true} for no changes at all.

Rules (research/traffic.md 1b, the owner's):
- The per-road structure stays: a road whose range has no weight still has none, and only the
  models a road's range already allows are reweighted; added models spread over every road of
  the set that spawns anything.
- A range shared with other sets is changed for all of them; those sets load the added models
  too (else the game would weight a model it doesn't load) and are budget-checked.
- Budget per set: at most traffic_ai.MAX_SET_MODELS (40) models, and at most
  slots - 1 - pot, where pot is the largest mission pot request any use of the set makes
  (spooling.isMissionVehiclesSpooled: "All chapters" + the named pot, or the given list; capped
  at 18 by spooling.requestToSpoolVehicle) and slots dsfcars.asi's [archive] vehicle_slots
  (35 without it); a set that scripts name but no progression entry lists (a mission switches
  to it mid-way) counts the pot cap, 18; a set no script names is never selected and has only
  the 40. Over the budget the chapter load waits forever for a refused pot model
  (research/traffic.md 3b). Ports past a set's budget are not dealt to it (reported).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.game import traffic_ai

AI_FILE = Path("media") / "san_franciscoAI.fchunk"
AI_COPY = Path("dsfcars") / "traffic" / "san_franciscoAI.fchunk"
TRAFFIC_LUA = Path("Common") / "UserLuaScripts" / "dsfcars" / "traffic.lua"
STORY_SETS = ["Exposition"] + [f"Chapter {i}" for i in range(1, 8)] + ["Epilogue"]
VANILLA_SLOTS = 35  # SSLP +0x18 of the vanilla vehicle archive (research/traffic.md 4)
POT_CAP = 18  # spoolingsystem.lua: local numberOfVehicleAllowed = 18
ALL_CHAPTERS = "All chapters"
WEIGHT_MODES = ("vanilla", "equal", "custom")
# The default, the owner's (2026-11-07): every story set filled with ports up to its slot
# budget, each port as likely on a road as an average vanilla car of that road ("mean"), the
# vanilla models' weights as the game has them.
DEFAULT_SET = {
    "ports": "fill",
    "share": "mean",
    "weights": "vanilla",
    "blend": 0.0,
    "custom": {},
    "vanilla_off": [],
    "extra": [],
}


# --- the game's Lua data ------------------------------------------------------------------


def lua_traffic_uses(scripts_package: Path) -> dict[str, int]:
    """{traffic set name: the largest mission pot request a use of it makes}, from the game's
    own scripts (media/ScriptsLuaScripts.fchunk): free roam of each story chapter (its set and
    its pot), and every mission, challenge and activity (settings.trafficSettings and
    missionVehiclePot in gamelogic\\progression\\data)."""
    from dsfforza.formats import lua_scripts
    from dsfforza.game import game_data

    env = {
        "module": lambda *_: None,
        "package": {"seeall": None},
        "vec": {"vector": lambda *a: a},
        "math": {"pi": math.pi},
    }
    pots: dict[str, list[int]] = {}
    uses: list[tuple[str, object]] = [(name, name) for name in STORY_SETS]
    for script in lua_scripts.read_package(scripts_package):
        path = (script["path"] or "").lower()
        if path.endswith("spoolingsystem\\data\\trafficspoolingdata.lua"):
            table = game_data.run_constant_chunk(script["data"], dict(env))
            pots = {k: [int(x) for x in v.values()] for k, v in table["missionSpecificVehiclePots"].items()}
        elif path.endswith(("missionprogression.lua", "challengeprogression.lua", "activitiesprogression.lua")):
            _walk_uses(game_data.run_constant_chunk(script["data"], dict(env)), uses)
    out: dict[str, int] = {}
    for traffic, pot in uses:
        if isinstance(pot, dict):  # a list of models: requested as it is
            wanted = {int(x) for x in pot.values()}
        else:
            wanted = set(pots.get(ALL_CHAPTERS, [])) | set(pots.get(pot, []) if isinstance(pot, str) else [])
        out[traffic] = max(out.get(traffic, 0), min(len(wanted), POT_CAP))
    return out


def _walk_uses(node, uses: list) -> None:
    if isinstance(node, dict):
        settings = node.get("settings") if isinstance(node.get("settings"), dict) else node
        if isinstance(settings, dict) and isinstance(settings.get("trafficSettings"), str):
            uses.append((settings["trafficSettings"], settings.get("missionVehiclePot")))
        for value in node.values():
            _walk_uses(value, uses)


# --- the plan -----------------------------------------------------------------------------


@dataclass
class SetReport:
    name: str
    models: int = 0
    budget: int = 0
    ports: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)
    off: list[int] = field(default_factory=list)
    weights: str = "vanilla"
    reaches: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def set_settings(ambient: dict, name: str) -> dict:
    out = dict(DEFAULT_SET)
    out.update(ambient.get("default") or {})
    out.update((ambient.get("sets") or {}).get(name) or {})
    if out["weights"] not in WEIGHT_MODES:
        raise ValueError(f"{name}: weights {out['weights']!r} is not one of {WEIGHT_MODES}")
    out["blend"] = 1.0 if out["weights"] == "equal" else float(out.get("blend") or 0.0)
    return out


def budget(set_name: str | None, slots: int, pots: dict[str, int]) -> int:
    """The most models the set may hold (module docstring)."""
    limit = traffic_ai.MAX_SET_MODELS
    if set_name is not None:
        # a set some script names but no mission, challenge or activity entry lists: a mission
        # switches to it mid-way (Chapter 1 Kidnapped) with a pot we don't know, so the cap
        limit = min(limit, slots - 1 - pots.get(set_name, POT_CAP))
    return limit


def road_weights(
    vanilla: dict[int, float], off: set[int], added: list[int], s: dict, factors: dict[int, float]
) -> dict[int, float]:
    """One road range's new weights: its allowed models (weight > 0) less `off`, blended between
    their vanilla shares and equal ones, then the added models at their share; custom factors;
    the range's total kept."""
    total = sum(vanilla.values())
    if total <= 0:
        return {}
    allowed = {m: w for m, w in vanilla.items() if w > 0 and m not in off}
    added = [m for m in dict.fromkeys(added) if m not in allowed]
    n = len(allowed) + len(added)
    if n == 0:
        raise ValueError("every model of a road would be off")
    b = min(max(float(s.get("blend") or 0.0), 0.0), 1.0)
    out: dict[int, float] = {}
    if allowed:
        t_allowed = sum(allowed.values())
        for m, w in allowed.items():
            out[m] = (1 - b) * w / t_allowed + b / len(allowed)
    if added:
        # each added model as likely as the road's average allowed model ("mean", and equal
        # weights): their share is k / (allowed + k); else the share asked for
        if b >= 1 or s.get("share") == "mean":
            share = len(added) / n
        else:
            share = min(max(float(s.get("share") or 0.0), 0.0), 0.95)
        if not allowed:
            share = 1.0
        for m in out:
            out[m] *= 1 - share
        for m in added:
            out[m] = share / len(added)
    if s.get("weights") == "custom":
        for m in out:
            out[m] *= max(float(factors.get(m, 1.0)), 0.0)
    scale = total / sum(out.values()) if sum(out.values()) > 0 else 0.0
    return {m: w * scale for m, w in out.items() if w > 0}


def plan(
    ai: traffic_ai.AiTraffic,
    ambient: dict,
    ports: list[int],
    key_ids: dict[str, int],
    *,
    slots: int,
    pots: dict[str, int],
    seed: int = 0,
) -> list[SetReport]:
    """Apply the ambient settings to `ai` (edited in place). `ports`: the IDs of the cars in
    traffic, in key order; `key_ids`: package key -> ID for settings that name keys."""
    planner = _Planner(ai, ambient, ports, key_ids, slots=slots, pots=pots, seed=seed)
    configured = list(STORY_SETS) + [n for n in (ambient.get("sets") or {}) if n not in STORY_SETS]
    reports = [r for r in (planner.apply_set(name) for name in configured) if r is not None]
    problems = ai.check()
    if problems:
        raise ValueError("; ".join(problems))
    return reports


class _Planner:
    def __init__(self, ai, ambient, ports, key_ids, *, slots, pots, seed):
        from dsfforza.game import traffic_sets

        self.ai, self.ambient, self.ports, self.key_ids = ai, ambient, ports, key_ids
        self.slots, self.pots = slots, pots
        # every set a mission, challenge or activity names (the pots' keys) gets its name, so its pot counts
        named = {traffic_sets.name_hash(n): n for n in pots}
        for ts in ai.sets:
            if ts.name is None and ts.hash in named:
                ts.name = named[ts.hash]
        self.deck = list(ports)
        random.Random(seed).shuffle(self.deck)
        self.touched: dict[int, str] = {}  # range -> the set whose settings changed it

    def factors(self, s: dict) -> dict[int, float]:
        out = {}
        for k, v in (s.get("custom") or {}).items():
            mid = int(k) if str(k).lstrip("-").isdigit() else self.key_ids.get(k)
            if mid is not None:
                out[mid] = float(v)
        return out

    def deal(self, want, room: int) -> list[int]:
        """The ports for one set: the keys asked for, the next `want` of the shuffled deck, or
        with "fill" as many as the set has room for."""
        if isinstance(want, list):
            return [self.key_ids[k] for k in want if self.key_ids.get(k) in self.ports]
        count = room if want == "fill" else int(want or 0)
        chosen = []
        for _ in range(min(count, len(self.deck))):
            chosen.append(self.deck[0])
            self.deck.append(self.deck.pop(0))
        return chosen

    def room(self, name, kept: int, reached) -> int:
        """How many models may be added: this set's budget and every reached set's."""
        rooms = [budget(name, self.slots, self.pots) - kept]
        rooms += [budget(u.name, self.slots, self.pots) - len(u.lists.get(fr, [])) for u, fr in reached]
        return max(min(rooms), 0)

    def apply_set(self, name: str) -> SetReport | None:
        ai = self.ai
        try:
            ts = ai.by_name(name)
        except KeyError:
            return None
        s = set_settings(self.ambient, name)
        rep = SetReport(name=name, weights=s["weights"] if s["weights"] != "vanilla" or not s["blend"] else "blend")
        vanilla_list = list(ts.lists.get(0, []))
        off = {int(x) for x in s.get("vanilla_off") or []}
        extra = [int(x) for x in s.get("extra") or [] if int(x) not in vanilla_list]
        ranges = {r for r in ai.road_ranges(ts, 0) if sum(ai.weights[r]) > 0}  # roads that spawn
        reached = [(u, fr) for u, fr in ai.users(ranges) if not (u.index == ts.index and fr == 0)]
        kept = [m for m in vanilla_list if m not in off]
        free = self.room(name, len(kept), reached)
        chosen = self.deal(s.get("ports"), max(free - len(extra), 0))
        added = (extra + chosen)[:free]
        if len(extra) + len(chosen) > free:
            rep.notes.append(f"{len(extra) + len(chosen) - free} of the added models left out: over the slot budget")
        if not added and not off and not s["blend"] and s["weights"] == "vanilla":
            return None
        clash = sorted({self.touched[r] for r in ranges if self.touched.get(r, name) != name})
        if clash:
            rep.notes.append(f"shares road tables with {clash}, already changed; left as they are")
            return rep
        factors = self.factors(s)
        for r in ranges:
            new = road_weights(ai.weighted(r), off, added, s, factors)
            if new:
                ai.set_range(r, new)
                self.touched[r] = name
        ai.set_list(ts, 0, kept + added)
        for u, fr in reached:
            lst = u.lists.get(fr, [])
            ai.set_list(u, fr, lst + [m for m in added if m not in lst])
        rep.models, rep.budget = len(kept) + len(added), budget(name, self.slots, self.pots)
        rep.ports = [m for m in chosen if m in added]
        rep.extra, rep.off = [m for m in extra if m in added], sorted(off)
        rep.reaches = sorted({u.name or f"{u.hash:08X}" for u, _ in reached})
        return rep


# --- the car manager's side ---------------------------------------------------------------

DLC_LIST = Path(__file__).resolve().parents[3] / "tools" / "batches" / "fm4-dlc.txt"
PLUGIN_INI = Path("plugins") / "dsfcars.ini"
SCRIPTS = Path("media") / "ScriptsLuaScripts.fchunk"


def dlc_media() -> set[str]:
    """The FM4 DLC cars' media names (tools/batches/fm4-dlc.txt): packages don't record their
    pack, so the "dlc" selector matches FM4 cars by these."""
    try:
        lines = DLC_LIST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.split()[0] for line in lines if line.strip() and not line.startswith("#")}


def plugin_slots(game: Path, capabilities: set[str] | None) -> int:
    """The vehicle spool slots the installed plugin gives: [archive] vehicle_slots of its ini when
    it can raise them (capability archive.slots), else the vanilla 35."""
    if not capabilities or "archive.slots" not in capabilities:
        return VANILLA_SLOTS
    import configparser

    ini = configparser.ConfigParser(inline_comment_prefixes=(";",), strict=False)
    try:
        ini.read(Path(game) / PLUGIN_INI, encoding="utf-8")
        return max(VANILLA_SLOTS, min(60, ini.getint("archive", "vehicle_slots", fallback=41)))
    except (OSError, ValueError, configparser.Error):
        return VANILLA_SLOTS


class TrafficControl:
    """The traffic settings of one Manager (car_manager.Manager) and the files made from them."""

    def __init__(self, manager):
        self.m = manager
        t = manager.state.setdefault("traffic", {})
        t.setdefault("cars", {})
        t.setdefault("ambient", {})
        t.setdefault("iv", {})
        self.t = t

    # --- which cars ---

    def cars(self) -> dict[str, dict]:
        """{key: {id, enabled, name, make, game, media, dlc, traffic, regions}} of the installed cars."""
        dlc = dlc_media()
        out = {}
        for key, c in sorted(self.m.state["cars"].items(), key=lambda kv: kv[0].casefold()):
            man = self.m.manifest(key) or {}
            src, names = man.get("source") or {}, man.get("names") or {}
            game = (src.get("game") or "").upper()
            media = src.get("media") or ""
            mine = self.t["cars"].get(key) or {}
            out[key] = {
                "id": c["id"],
                "enabled": c["enabled"],
                "name": names.get("display") or key,
                "make": (names.get("make") or "").upper(),
                "game": game,
                "media": media,
                "dlc": game == "FM4" and media in dlc,
                "traffic": mine.get("traffic", True),
                "regions": mine.get("regions"),
            }
        return out

    def select(self, selectors: list[str]) -> list[str]:
        """Keys for selectors: a key, make:<MAKE>, game:FH1|FM4, dlc, all."""
        cars = self.cars()
        out: list[str] = []
        for sel in selectors:
            low = sel.lower()
            if low == "all":
                hits = list(cars)
            elif low == "dlc":
                hits = [k for k, c in cars.items() if c["dlc"]]
            elif low.startswith("make:"):
                hits = [k for k, c in cars.items() if c["make"] == sel[5:].upper()]
            elif low.startswith("game:"):
                hits = [k for k, c in cars.items() if c["game"] == sel[5:].upper()]
            elif sel in cars:
                hits = [sel]
            else:
                raise SystemExit(f"{sel}: no installed car or selector (a key, make:<MAKE>, game:FH1|FM4, dlc, all)")
            out += [k for k in hits if k not in out]
        return out

    def in_traffic(self) -> list[tuple[int, str]]:
        """(ID, key) of the enabled cars with traffic on, in key order."""
        return [(c["id"], k) for k, c in self.cars().items() if c["enabled"] and c["traffic"]]

    # --- settings ---

    def set_traffic(self, keys: list[str], *, on: bool) -> None:
        for key in keys:
            entry = self.t["cars"].setdefault(key, {})
            if on:
                entry.pop("traffic", None)
            else:
                entry["traffic"] = False
            if not entry:
                del self.t["cars"][key]

    def pin(self, keys: list[str], regions: list[int] | None) -> None:
        for r in regions or []:
            if not 1 <= r <= 17:
                raise SystemExit(f"region {r}: the game has regions 1 to 17")
        for key in keys:
            entry = self.t["cars"].setdefault(key, {})
            if regions:
                entry["regions"] = sorted(set(regions))
            else:
                entry.pop("regions", None)
            if not entry:
                del self.t["cars"][key]

    def set_ambient(self, set_name: str | None, **fields) -> None:
        """Change the default (set_name None) or one set's settings; a field set to None goes
        back to the default's."""
        amb = self.t["ambient"]
        amb.pop("off", None)
        if set_name is None:
            target = amb.setdefault("default", {})
        else:
            target = amb.setdefault("sets", {}).setdefault(set_name, {})
        for k, v in fields.items():
            if k not in DEFAULT_SET:
                raise SystemExit(f"{k}: not a traffic set setting ({', '.join(DEFAULT_SET)})")
            if v is None:
                target.pop(k, None)
            else:
                target[k] = v
        if set_name is not None and not target:
            del amb["sets"][set_name]
        set_settings(amb, set_name or STORY_SETS[0])  # validates the mode

    def shuffle(self) -> None:
        self.t["ambient"]["seed"] = int(self.t["ambient"].get("seed", 0)) + 1

    def ambient_off(self) -> None:
        """No set changes at all, none from the default either, until a set setting is made again."""
        self.t["ambient"] = {"off": True, "seed": self.t["ambient"].get("seed", 0)}

    # --- the files ---

    def base(self) -> Path:
        """The folder whose game files the copy starts from (Manager.gui_base: the game folder,
        or the game an overlay is laid over in the VM tests)."""
        return Path(self.m.gui_base)

    def game_ai(self) -> Path:
        return self.base() / AI_FILE

    def facts(self) -> tuple[int, dict[str, int]]:
        from dsfforza.build import car_manager

        slots = plugin_slots(self.m.game, car_manager.plugin_capabilities(self.m.game / car_manager.PLUGIN))
        try:
            pots = lua_traffic_uses(self.base() / SCRIPTS)
        except (OSError, ValueError, KeyError) as e:
            raise SystemExit(
                f"{self.base() / SCRIPTS}: can't read the mission pots ({e}); no traffic sets written"
            ) from e
        return slots, pots

    def plan(self) -> tuple[traffic_ai.AiTraffic, list[SetReport]]:
        ai = traffic_ai.AiTraffic.read(self.game_ai(), traffic_ai.known_names())
        ids = self.in_traffic()
        slots, pots = self.facts()
        key_ids = {k: c["id"] for k, c in self.cars().items()}
        seed = int(self.t["ambient"].get("seed", 0))
        reports = plan(ai, self.t["ambient"], [i for i, _ in ids], key_ids, slots=slots, pots=pots, seed=seed)
        return ai, reports

    def ambient_changes(self) -> bool:
        """Whether the sets are changed: unless switched off, the default (DEFAULT_SET) does."""
        return not self.t["ambient"].get("off")

    def write(self) -> list[SetReport]:
        """The AI copy and its redirect line (neither when no set changes), and traffic.lua."""
        from dsfforza.build import car_gui

        game = self.m.game
        copy = game / AI_COPY
        reports: list[SetReport] = []
        changed = False
        ai = None
        if self.ambient_changes() and self.game_ai().exists():
            try:
                ai, reports = self.plan()
            except (SystemExit, ValueError, OSError) as e:
                # the game's scripts or AI file can't be read: no copy; the command that saved goes on
                self.m.log(f"traffic sets left as the game has them: {e}")
        if ai is not None and (ai.edits.ranges or ai.edits.lists):
            copy.parent.mkdir(parents=True, exist_ok=True)
            temp = copy.with_name(copy.name + ".tmp")
            temp.write_bytes(ai.to_bytes())
            temp.replace(copy)
            changed = True
        if not changed:
            copy.unlink(missing_ok=True)
            if copy.parent.exists() and not any(copy.parent.iterdir()):
                copy.parent.rmdir()
        car_gui.set_redirect(game, AI_FILE, AI_COPY if changed else None)
        self.write_lua()
        return reports

    def write_lua(self) -> None:
        """traffic.lua: register.lua's per-car interesting-vehicle settings, by model ID."""
        rows = []
        for key, c in self.cars().items():
            if not c["enabled"]:
                continue
            fields = []
            if not c["traffic"]:
                fields.append("traffic = false")
            if c["regions"]:
                fields.append("regions = { " + ", ".join(str(r) for r in c["regions"]) + " }")
            if fields:
                rows.append(f"\t\t[{c['id']}] = {{ {', '.join(fields)} }},  -- {key}")
        iv = self.t["iv"]
        ports = "false" if iv.get("ports") is False else "true"
        text = "\n".join(
            [
                "-- Written by dsff car_manager (src/dsfforza/build/traffic.py); do not edit.",
                "-- Interesting-vehicle settings of the installed cars, read by register.lua.",
                "return {",
                f"\tports = {ports},",
                f"\tvanilla = {'false' if iv.get('vanilla') is False else 'true'},",
                f"\tper_region = {int(iv.get('per_region', 24))},",
                "\tcars = {",
                *rows,
                "\t},",
                "}",
                "",
            ]
        )
        path = self.m.game / TRAFFIC_LUA
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def clear(self) -> None:
        """Uninstall: the copy, its redirect line and traffic.lua go."""
        from dsfforza.build import car_gui

        (self.m.game / AI_COPY).unlink(missing_ok=True)
        car_gui.set_redirect(self.m.game, AI_FILE, None)
        (self.m.game / TRAFFIC_LUA).unlink(missing_ok=True)

    def status(self) -> list[str]:
        out = []
        cars = self.cars()
        on = [k for k, c in cars.items() if c["enabled"] and c["traffic"]]
        out.append(f"cars in traffic: {len(on)} of {sum(c['enabled'] for c in cars.values())} enabled")
        pinned = {k: c["regions"] for k, c in cars.items() if c["regions"]}
        if pinned:
            out.append("pinned to interesting-vehicle regions: " + ", ".join(f"{k} {v}" for k, v in pinned.items()))
        iv = self.t["iv"]
        out.append(
            f"interesting vehicles: ports {'off' if iv.get('ports') is False else 'on'}, "
            f"the game's {'off' if iv.get('vanilla') is False else 'on'}, "
            f"{int(iv.get('per_region', 24))} per region (at most 128 in a region with the vanilla IDs)"
        )
        ai_path = self.game_ai()
        if ai_path.exists():
            vanilla = traffic_ai.sha256(ai_path) == traffic_ai.VANILLA_SHA256
            note = "vanilla" if vanilla else "NOT vanilla (another mod changed it; our copy is made from it)"
            out.append(f"{ai_path}: {note}")
        if not self.ambient_changes():
            out.append("ambient traffic: as the game has it (no settings)")
            return out
        ai, reports = self.plan()
        slots, _ = self.facts()
        out.append(f"ambient traffic ({slots} vehicle spool slots):")
        for r in reports:
            line = f"  {r.name}: {r.models} models of {r.budget}, weights {r.weights}"
            if r.ports:
                share = ai.share(ai.by_name(r.name))
                line += f", ports {r.ports} ({sum(share.get(m, 0.0) for m in r.ports):.0%} of its roads)"
            if r.extra:
                line += f", added vanilla {r.extra}"
            if r.off:
                line += f", off {r.off}"
            if r.reaches:
                line += f"; also changes {', '.join(r.reaches)}"
            out.append(line + "".join(f"; {n}" for n in r.notes))
        return out
