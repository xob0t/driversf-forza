"""The cockpit carbin's cabin in the clean exterior entry (issue #429).

Forza's LOD 0 of a car is two carbins: `<car>_lod0.carbin` holds the outside without
the cabin, the bonnet, the mirrors and the front glass, and `<car>_cockpit.carbin` holds
exactly those (research/cockpit.md, "Forza's LOD 0 is the lod0 carbin and the cockpit
carbin"). The main carbin's LOD 1 cabin is the far stand-in: about a thousand triangles,
no steering column, its wheel on the dash, its UVs on `nodamage.xds` artwork a livery can
paint over. The chase views see the player's car from 5.8 m, where Forza draws LOD 0.

With `ChaseCabin`, convert_interior also culls the cockpit's cabin sections (not the
outside parts, the glass or the needles: the exterior has its own) to what the chase
camera sees through the windows from any side (`chase_eyes`), plans the interior atlas
for them too, and hands them over with its binding. The exterior entry then draws them
with the interior entry's own materials and atlas, in place of the LOD-1 cabin; its
steering wheel is the cockpit's, skinned to the steering record, on the hinge.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.geometry import LIGHT_SECTIONS, SKIP_SECTIONS, promote_lod
from dsfforza.convert.cockpit.visibility import cull_sections, visible_triangles

# The main carbin's cabin sections, which the cockpit's replace in the exterior entry.
LOD1_CABIN = ("interior", "seatl", "seatr", "steering_wheel")
# DSF's chase camera (cameraMode Normal) in the car's frame, logged by the probe
# (clonetest.lua) on runs vm1-20261031-093257 and -093720: 5.5 to 5.8 m behind the car's
# origin, 1.65 to 1.77 m above it.
CHASE_DISTANCE = 5.8
CHASE_HEIGHT = 1.7
# The ring of eyes about the car at that distance: the chase camera swings with the
# steering, and Shift, the garage and replays show the car from every side.
CHASE_AZIMUTHS = 12


def chase_eyes(place, rear: bool = False) -> list[tuple]:
    """Eyes (Forza space) on a ring CHASE_DISTANCE about the car's origin at CHASE_HEIGHT;
    `rear`: only those beside and behind the car (DSF z <= 0), where the chase camera
    looks from as it swings with the steering."""
    eyes = []
    for k in range(CHASE_AZIMUTHS):
        a = 2 * math.pi * k / CHASE_AZIMUTHS
        dsf = (CHASE_DISTANCE * math.sin(a), CHASE_HEIGHT, CHASE_DISTANCE * math.cos(a))
        if rear and dsf[2] > 1e-9:
            continue
        eyes.append(place.forza(dsf))
    return eyes


@dataclass
class ChaseCabin:
    """In: the conversion's placement and the main carbin's body (the occluder) at the
    exterior's LOD. Out (convert_interior): the culled cabin sections, the interior's
    binding and shared materials, and a report."""

    place: object
    body: list
    lod: int
    sections: list = field(default_factory=list)  # culled to the whole ring
    rear_sections: list = field(default_factory=list)  # culled to its rear half (an exterior over its cap)
    binding: object = None
    shared: dict | None = None
    layout: dict | None = None  # material UID -> (stride, MODL flag), the interior's included
    report: dict = field(default_factory=dict)


def cabin_sections(static: dict) -> list:
    """The cockpit carbin's cabin: its static sections minus the outside parts
    (fm.COCKPIT_EXTERIOR_SECTIONS), the upgrades (SKIP_SECTIONS), the lamps, and every
    glass subsection (the exterior entry has the main carbin's)."""
    out = []
    for name, s in static.items():
        if fm.COCKPIT_EXTERIOR_SECTIONS.match(name) or name in SKIP_SECTIONS or LIGHT_SECTIONS.search(name):
            continue
        subs = [u for u in s.subsections if u.lod == 0 and fm.classify_cockpit(u.material, name)[0] != fm.GLASS]
        if subs:
            s2 = copy.copy(s)
            s2.subsections = subs
            out.append(s2)
    return out


def cull_for_chase(chase: ChaseCabin, static: dict, resolution: int = 512) -> tuple[list, list]:
    """cabin_sections culled to what chase_eyes see through the main body's windows (the
    body at the exterior's LOD occludes, its glass doesn't), from the whole ring and from
    its rear half. Small triangles count as seen within 0.5% of the depth a pixel shows
    (3 cm at the chase distance; the head view's 3% would be 17 cm)."""
    cabin = cabin_sections(static)
    occluders = []
    for s in chase.body:
        if s.name.lower() in LOD1_CABIN or s.name.lower().startswith(("wheel", "blur")):
            continue
        p = promote_lod(s, chase.lod) if any(u.lod == chase.lod for u in s.subsections) else None
        if p is not None:
            occluders.append(p)
    kept = visible_triangles(cabin + occluders, chase_eyes(chase.place), resolution, near_keep=0.0, tiny_slack=0.005)
    kept = {k: v for k, v in kept.items() if k[0] < len(cabin)}
    culled = cull_sections(cabin, kept)
    before = sum(len(u.triangles()) for s in cabin for u in s.subsections)
    after = sum(len(u.triangles()) for s in culled for u in s.subsections)
    lod1 = sum(
        len(u.triangles())
        for s in chase.body
        if s.name.lower() in LOD1_CABIN
        for u in s.subsections
        if u.lod == chase.lod
    )
    chase.report = {"cockpit_cabin_triangles": before, "seen_from_outside": after, "lod1_cabin_triangles": lod1}
    return [s for s in culled if s.subsections]
