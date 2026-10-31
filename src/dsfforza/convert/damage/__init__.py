"""Vanilla-like damage for converted Forza cars: panels, crush morph, hinges.

How a vanilla DSF car damages (research/damage.md, confirmed-static from the
shared vehicle shaders in SSIC and the vanilla geometry):

- Vertex semantic 9 bytes 0 and 1 pick the panel. Byte 0 (UPVH record +0x34)
  indexes g_PanelTransforms (a 3x4 matrix per panel: hinge swing, detached
  debris); byte 1 (record +0x2A) indexes g_v4PerPanelData, the panel's damage
  state. Byte 2 is the lamp, byte 3 is 1.
- Semantic 12 ("vec3DamagePos", half4) is the panel's fully crushed position.
  The vertex shader draws lerp(position, s12, fDamage), with
  fDamage = PerPanelData[byte 1].x - offset * x (a per-panel amount with a
  left-right gradient). Vanilla cars push the nose back by about 6% of the car
  length and the tail forward by about 2%, buckle the bonnet up and dent the
  doors by a few centimetres.
- Semantic 5 bytes 2 and 3 are the normal's polar/azimuth change at full
  damage (angle = byte / 255 * 2pi - pi), so dents shade.
- MODL part 0 of the full entry holds every panel's triangles in one
  submodel per material (the intact car, skinned by panel); each panel's own
  MODL part holds the same vertices again with its own index lists. The
  exterior entry holds the merged part 0 only.

Converted cars had every vertex on the chassis panel with s12 = position: no
crush, no swinging bonnet/boot/bumpers, nothing to fall off. This module
assigns each Forza triangle to one of the host's panels by role (Forza
section name first, then the nearest host panel), copies the host's crush
field onto the Forza vertices (inverse-distance weighted, in car-normalised
coordinates, scaled to the Forza car), writes the normal deltas, builds the
per-panel MODL parts, and moves the host's panel hinges, rigid bodies and
collision boxes onto the Forza panels.

Use: forza2dsf.ConvertOptions(damage=True) (port_car.py --damage). CLI:

    uv run dsff damage panels <archive> --id 266          # the host's panel records
    uv run dsff damage render <archive> --id 304 --png out/x.png [--kind 0|2] [--by part|panel]
                                  [--damage 1] [--parts 2,4,11,12]
    uv run dsff damage check <archive> --id 304           # structural checks of a converted car
"""

from __future__ import annotations

from dsfforza.convert.damage.cli import (
    main,
)
from dsfforza.convert.damage.core import (
    FACING_WEIGHT,
    PAINT_REACH,
    PANEL_FACING,
    Box,
    Grid,
    _cross,
    _dot,
    _norm,
    _sub,
    load_mesh,
)
from dsfforza.convert.damage.exposed import (
    BAY_CELL,
    BAY_CLEARANCE,
    BUMPER_CLEARANCE,
    EXPOSED_ZONES,
    ZONE_SHARE,
    ZONE_STEP,
    _tri_samples,
    _vote_samples,
    _zones_at,
    engine_placement,
    exposed_zones,
    split_exposed,
    zone_maps,
    zone_of,
    zone_vote,
    zone_votes,
)
from dsfforza.convert.damage.host import (
    HostField,
    Panel,
    glass_name,
)
from dsfforza.convert.damage.host_cache import (
    HOST_CACHE_KEEP_DAYS,
    DamageOptions,
    _canonical,
    _prune_host_cache,
    host_setup,
)
from dsfforza.convert.damage.parts import (
    part_components,
    part_relabel,
    part_stats,
)
from dsfforza.convert.damage.plan import (
    DamagePlan,
)
from dsfforza.convert.damage.tree import (
    _collision_bottom,
    _floor_collision,
    _map_collision,
    _parents,
    _primitive_bottom,
    _read_tree_fields,
    _tree_fields,
    move_subtree,
    trimmed_box,
)
from dsfforza.convert.damage.uv import (
    Uv1Field,
    closest_on_triangle,
    closest_on_triangles,
)

__all__ = [
    "BAY_CELL",
    "BAY_CLEARANCE",
    "BUMPER_CLEARANCE",
    "EXPOSED_ZONES",
    "FACING_WEIGHT",
    "HOST_CACHE_KEEP_DAYS",
    "PAINT_REACH",
    "PANEL_FACING",
    "ZONE_SHARE",
    "ZONE_STEP",
    "Box",
    "DamageOptions",
    "DamagePlan",
    "Grid",
    "HostField",
    "Panel",
    "Uv1Field",
    "_canonical",
    "_collision_bottom",
    "_cross",
    "_dot",
    "_floor_collision",
    "_map_collision",
    "_norm",
    "_parents",
    "_primitive_bottom",
    "_prune_host_cache",
    "_read_tree_fields",
    "_sub",
    "_tree_fields",
    "_tri_samples",
    "_vote_samples",
    "_zones_at",
    "closest_on_triangle",
    "closest_on_triangles",
    "engine_placement",
    "exposed_zones",
    "glass_name",
    "host_setup",
    "load_mesh",
    "main",
    "move_subtree",
    "part_components",
    "part_relabel",
    "part_stats",
    "split_exposed",
    "trimmed_box",
    "zone_maps",
    "zone_of",
    "zone_vote",
    "zone_votes",
]
