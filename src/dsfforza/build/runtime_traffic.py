"""Live chapter pools for the native picker. The AI file stays vanilla in memory;
the plugin samples these full weight rows and streams nonresident choices.
Native layouts/lifetime evidence: research/runtime-traffic-pools.md.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from dsfforza.game import traffic_ai

if TYPE_CHECKING:
    from dsfforza.build.traffic import TrafficControl

MAGIC = b"DSFPOOL2"
# magic, record count, choice count, package-ID count, the vehicle slots the caps were planned for
HEADER = struct.Struct("<8sIIII")
# set hash, frequency, native start/count/hash, choice start/count, live cap, reserve, total.
# reserve: the slots the rest of the cap rule holds (live_reserve); the plugin lowers live cap to
# slots - reserve when the game has fewer slots than planned (mod_traffic.cpp live_cap).
ROW = struct.Struct("<9If")
PAIR = struct.Struct("<IfI")  # model ID, weight, flags: package=1, extra native model=2
POOL_FILE = Path("dsfcars") / "traffic" / "pools.bin"
MARKER = b"DSFCARS-TRAFFIC-POOLS: 2"
# 005A6D20: the nontraffic loaded-node storage is +5B0..+998, 20 bytes/node.
MISSION_NODES = (0x998 - 0x5B0) // 20
# 007EB860: native interesting-vehicle desired count is capped at ten models.
MAX_IV_MODELS = 10


def live_reserve(loaded: int, pot: int) -> int:
    """The spool slots a set's streaming budget leaves to others: its native list (loaded models),
    the mission pot, the player's car and the interesting vehicles."""
    return loaded + pot + 1 + MAX_IV_MODELS


def live_cap(slots: int, loaded: int, pot: int, ports: object = None) -> int:
    """The streaming budget: what the spool slots and the 50 nontraffic nodes leave after
    live_reserve, at most a numeric ports setting. dsfcars.asi applies the same rule with the slots
    the game got (min(cap, slots - reserve)), which equals this when they are fewer than planned."""
    cap = max(0, min(slots - live_reserve(loaded, pot), MISSION_NODES - live_reserve(0, pot)))
    if isinstance(ports, int):
        cap = min(cap, max(ports, 0))
    return cap


def supported(plugin: Path) -> bool:
    """Traffic is a plugin feature, not a requirement added to every car package."""
    try:
        return MARKER in plugin.read_bytes()
    except OSError:
        return False


def fingerprint(data: bytes) -> int:
    """FNV-1a over native range/pair bytes; a changed traffic mod must not use stale rows."""
    value = 2166136261
    for byte in data:
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return value


def native_fingerprint(ai: traffic_ai.AiTraffic, index: int) -> int:
    start, count, total = ai.ranges[index]
    data = struct.pack("<IIf", start, count, total)
    data += b"".join(struct.pack("<If", mid, weight) for mid, weight in zip(ai.columns, ai.weights[index]))
    return fingerprint(data)


@dataclass
class Report:
    name: str
    candidates: int
    baseline: int
    live_limit: int
    notes: list[str]


def build(control: TrafficControl) -> tuple[bytes, list[Report]]:
    """Serialize full weighted pools, never a preselected batch. Caps reserve the native
    mission pot/player and the 50 nontraffic nodes; no timer or fitted population target.
    """
    from dsfforza.build import traffic

    ai = traffic_ai.AiTraffic.read(control.game_ai(), traffic_ai.known_names())
    slots, pots = control.facts()
    cars = control.cars()
    package_ids = sorted({c["id"] for c in cars.values() if c["enabled"]})
    package_id_set = set(package_ids)
    key_ids = {key: c["id"] for key, c in cars.items()}
    ambient = control.t["ambient"]
    rows, pairs, reports = [], bytearray(), []
    shared: dict[bytes, int] = {}
    for ts in ai.sets:
        name = ts.name or f"{ts.hash:08X}"
        settings = traffic.set_settings(ambient, name)
        pool = control.pool_keys(settings["pool"])
        if isinstance(settings["ports"], list):
            pool = [key for key in pool if key in settings["ports"]]
        if settings["ports"] == 0:
            pool = []
        wanted = [key_ids[key] for key in pool]
        baseline = len(set(ts.lists.get(0, [])))
        pot = pots.get(name, traffic.POT_CAP)
        limit = live_cap(slots, baseline, pot, settings["ports"])
        reports.append(Report(name, len(pool), baseline, limit, []))
        for frequency, loaded in ts.lists.items():
            cap = live_cap(slots, len(set(loaded)), pot, settings["ports"])
            reserve = live_reserve(len(set(loaded)), pot)
            for index in sorted(ai.road_ranges(ts, frequency)):
                native = ai.weighted(index)
                if not native:
                    continue  # a road the game leaves empty stays empty
                choices = full_weights(settings, native, wanted, key_ids)
                packed = b"".join(
                    PAIR.pack(mid, weight, 1 if mid in package_id_set else 2 if mid not in loaded else 0)
                    for mid, weight in choices.items()
                )
                if packed not in shared:
                    shared[packed] = len(pairs) // PAIR.size
                    pairs.extend(packed)
                start, count, _total = ai.ranges[index]
                rows.append(
                    ROW.pack(
                        ts.hash,
                        frequency,
                        start,
                        count,
                        native_fingerprint(ai, index),
                        shared[packed],
                        len(choices),
                        cap,
                        reserve,
                        sum(choices.values()),
                    )
                )
    ids = struct.pack(f"<{len(package_ids)}I", *package_ids)
    return HEADER.pack(MAGIC, len(rows), len(pairs) // PAIR.size, len(package_ids), slots) + ids + b"".join(
        rows
    ) + pairs, reports


def full_weights(settings: dict, native: dict, wanted: list[int], key_ids: dict) -> dict[int, float]:
    """Use the existing road-weight math over the entire configured pool."""
    from dsfforza.build import traffic

    off = set(settings.get("vanilla_off") or [])
    added = list(dict.fromkeys([*(settings.get("extra") or []), *wanted]))
    if not (set(native) - off or added):
        return {}
    factors = {}
    for key, value in (settings.get("custom") or {}).items():
        mid = int(key) if str(key).lstrip("-").isdigit() else key_ids.get(key)
        if mid is not None:
            factors[mid] = float(value)
    return traffic.road_weights(native, off, added, settings, factors)


def preview(control: TrafficControl, name: str, ambient: dict) -> tuple[dict[int, float], dict[int, float], Report]:
    """Full-pool road shares, not a generated resident subset. Nothing is serialized to AIEX."""
    from dsfforza.build import traffic

    ai = traffic_ai.AiTraffic.read(control.game_ai(), traffic_ai.known_names())
    ts = ai.by_name(name)
    native = ai.share(ts)
    settings = traffic.set_settings(ambient, name)
    pool = control.pool_keys(settings["pool"])
    if isinstance(settings["ports"], list):
        pool = [key for key in pool if key in settings["ports"]]
    if settings["ports"] == 0:
        pool = []
    key_ids = {key: c["id"] for key, c in control.cars().items()}
    wanted = [key_ids[key] for key in pool]
    slots, pots = control.facts()
    baseline = len(set(ts.lists.get(0, [])))
    pot = pots.get(name, traffic.POT_CAP)
    limit = live_cap(slots, baseline, pot, settings["ports"])
    for index in ai.road_ranges(ts, 0):
        weights = ai.weighted(index)
        if weights:
            ai.set_range(index, full_weights(settings, weights, wanted, key_ids))
    return native, ai.share(ts), Report(name, len(pool), baseline, limit, [])


def write(control: TrafficControl) -> list[Report]:
    """Atomically replace live weights and remove the old static traffic redirect."""
    from dsfforza.build import car_gui, traffic

    payload, reports = build(control)
    target = control.m.game / POOL_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".tmp")
    temp.write_bytes(payload)
    temp.replace(target)
    (control.m.game / traffic.AI_COPY).unlink(missing_ok=True)
    (control.m.game / traffic.AI_SLOTS).unlink(missing_ok=True)
    car_gui.set_redirect(control.m.game, traffic.AI_FILE, None)
    control.write_lua()
    return reports
