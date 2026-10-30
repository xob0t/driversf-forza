"""Cockpit view for converted Forza cars: the interior entry (N+1000), the
steering wheel hinge, the gauges and the head camera.

What DSF needs (research/cockpit.md):

- The cockpit view draws the interior VEHC entry (kind 1, MODL mask 64)
  alone. It is self-contained: cabin, seats, dashboard, steering wheel,
  A-pillars and roof lining, glass, and a low-detail copy of the outer body
  (hood, rear deck) for what shows through the windows. The full entry has
  no mask-64 LODs.
- The steering wheel is not a separate part. Its vertices are skinned to the
  hierarchy: vertex semantic 9 = (record +0x34, record +0x2A, light, 1) of
  the steering-wheel record (the part on the hinge whose pivot is marker
  role 47; Desanne: record 11, hinge 0, s9 = (1, 1, 0, 1)). The hinge axis
  is the steering column.
- The needles are one stride-20 submodel in their own MODL part (the
  "Dials" material, alpha-tested); vertex semantic 8 component 2 is the
  gauge id, and UPVH's "seat" table is really the gauge table: pivot,
  dial-plane quaternion, u32 gauge id, u32 sweep. Each frame the engine
  turns needle `id` about the pivot, right-handed about the quaternion's
  z axis (the dial normal, into the dash: clockwise for the driver), by
  sweep degrees x input (FUN_008A2610 via g_InstrumentTransforms):
  id 1 tachometer, input clamp(rpm, p40, p42) / 5000; id 2 speedometer,
  input clamp(mph, p606, p605) / 80 (FUN_00891670). Ids 3..7 get no input.
  So the u32 is the needle's degrees per 5000 rpm or per 80 mph, the rest
  pose is 0 rpm / 0 mph, and VO3D p605 is the speedometer's last mark
  (vanilla DeLorean 88, Eldorado 120; default 255).
- The head camera (VO3D 430..432, FUN_0088DF80 case 0x14) is
  (p430 - marker47.x, p431, p432) in a frame turned 180 degrees about y,
  so in model space eye = (marker47.x - p430, p431, -p432).

Forza side: <car>_cockpit.carbin (LOD 0 only; the existing carbin parser
reads it), nodamage_LOD0 / lights_LOD0 / interior_LOD0 textures, and
gamedb CameraOverrides.CamCockpitOffset{X,Y,Z}: the eye relative to the
steering wheel centre, with +z forward (so eye = wheel + (x, y, -z) in
Forza space, whose front is -z). Zero rows mean "no override"; then the
median of the non-zero rows is used.

    uv run dsff cockpit fit out/fh1/NIS_370Z_10 NIS_370Z_10 --gamedb out/db/fh1_gamedb.slt
    uv run dsff cockpit render <archive> --id 304 --png out/cockpit.png [--eye-view out/eye.png]

As a library: `convert_interior(...)` (called from forza2dsf.convert with
ConvertOptions.cockpit) rebuilds the interior entry and patches the full
entry's UPVH and VO3D.
"""

from __future__ import annotations

from dsfforza.convert.cockpit.cli import (
    main,
)
from dsfforza.convert.cockpit.displays import (
    DASH_PANEL,
    HUD_DOWN_DEG,
    HUD_LEFT_DEG,
    HUD_RIGHT_DEG,
    POD_GAUGE_CLEAR,
    POD_HAND_CLEAR,
    POD_HUD_PASSES,
    POD_PASSES,
    POD_WHEEL_CLEAR,
    WHEEL_DISPLAY_LIFT,
    WHEEL_DISPLAY_SCALES,
    WHEEL_DISPLAY_SMALL_SCALES,
    display_patches,
    drop_unturned,
    flatten_face,
    place_pod,
    pod_display,
    rim_grips,
    virtual_gauges,
    wheel_display_pod,
)
from dsfforza.convert.cockpit.fit import (
    fit,
    gauge_params,
)
from dsfforza.convert.cockpit.geometry import (
    DASHBOARD_INSTRUMENT,
    DISPLAY_FACE,
    FORCE_FLAT,
    GAUGE_IDS,
    GAUGE_UNITS,
    LIGHT_SECTIONS,
    NEEDLE,
    PARK_MID,
    SHELL_SKIP,
    SKIP_SECTIONS,
    STEERING_MARKER_ROLE,
    CockpitFit,
    Gauge,
    Wheel,
    _add,
    _cross,
    _dot,
    _mean,
    _norm,
    _scale,
    _sub,
    align_main_wheel,
    display_face,
    fit_gauge,
    fit_wheel,
    fit_wheel_points,
    load_cockpit,
    promote_lod,
    quat_from_to,
    quat_rotate,
    section_points,
    uv_map_stats,
    with_display_faces,
)
from dsfforza.convert.cockpit.hierarchy import (
    CLUSTER_ROLE,
    DRIVER_SEAT_ROLE,
    FLEET_EYE_ABOVE_SEAT,
    GAUGE_ROLES,
    GAUGE_SIGHT_CLEARANCE,
    GRIP_ALONG_PARAM,
    GRIP_PARAMS,
    GRIP_RADIUS_PARAM,
    HAND_PARAMS,
    VANILLA_COLUMN,
    VANILLA_MARKER_FROM_SEAT,
    VANILLA_RIM_RADIUS,
    GaugeSight,
    _rim_nearest,
    add_gauge_records,
    camera_eye,
    camera_params,
    gauge_records,
    gauge_sight_points,
    grip_params,
    grip_shift,
    hand_grip_error,
    hand_grips,
    hand_params,
    hand_shift,
    head_camera,
    host_cockpit,
    patch_hierarchy,
    patch_seats,
    patch_vo3d,
    seat_anchor,
    seat_for_marker,
    seat_markers,
)
from dsfforza.convert.cockpit.interior import (
    CockpitBinding,
    CockpitOptions,
    InteriorMaterials,
    _needle_vertex,
    convert_interior,
    host_interior,
)
from dsfforza.convert.cockpit.previews import (
    dials_material,
    needle_check,
    needle_gauge_ids,
)
from dsfforza.convert.cockpit.visibility import (
    CUBE_FACES,
    LID_RAYS,
    NEAR_PLANE,
    RASTER_TILE,
    GlassSight,
    _clip_near,
    _face_parts,
    _raster,
    _screen_parts,
    shell_blocks_view,
    shell_closes,
    shell_lids_cockpit,
    visible_triangles,
)

__all__ = [
    "CLUSTER_ROLE",
    "CUBE_FACES",
    "DASHBOARD_INSTRUMENT",
    "DASH_PANEL",
    "DISPLAY_FACE",
    "DRIVER_SEAT_ROLE",
    "FLEET_EYE_ABOVE_SEAT",
    "FORCE_FLAT",
    "GAUGE_IDS",
    "GAUGE_ROLES",
    "GAUGE_SIGHT_CLEARANCE",
    "GAUGE_UNITS",
    "GRIP_ALONG_PARAM",
    "GRIP_PARAMS",
    "GRIP_RADIUS_PARAM",
    "HAND_PARAMS",
    "HUD_DOWN_DEG",
    "HUD_LEFT_DEG",
    "HUD_RIGHT_DEG",
    "LID_RAYS",
    "LIGHT_SECTIONS",
    "NEAR_PLANE",
    "NEEDLE",
    "PARK_MID",
    "POD_GAUGE_CLEAR",
    "POD_HAND_CLEAR",
    "POD_HUD_PASSES",
    "POD_PASSES",
    "POD_WHEEL_CLEAR",
    "RASTER_TILE",
    "SHELL_SKIP",
    "SKIP_SECTIONS",
    "STEERING_MARKER_ROLE",
    "VANILLA_COLUMN",
    "VANILLA_MARKER_FROM_SEAT",
    "VANILLA_RIM_RADIUS",
    "WHEEL_DISPLAY_LIFT",
    "WHEEL_DISPLAY_SCALES",
    "WHEEL_DISPLAY_SMALL_SCALES",
    "CockpitBinding",
    "CockpitFit",
    "CockpitOptions",
    "Gauge",
    "GaugeSight",
    "GlassSight",
    "InteriorMaterials",
    "Wheel",
    "_add",
    "_clip_near",
    "_cross",
    "_dot",
    "_face_parts",
    "_mean",
    "_needle_vertex",
    "_norm",
    "_raster",
    "_rim_nearest",
    "_scale",
    "_screen_parts",
    "_sub",
    "add_gauge_records",
    "align_main_wheel",
    "camera_eye",
    "camera_params",
    "convert_interior",
    "dials_material",
    "display_face",
    "display_patches",
    "drop_unturned",
    "fit",
    "fit_gauge",
    "fit_wheel",
    "fit_wheel_points",
    "flatten_face",
    "gauge_params",
    "gauge_records",
    "gauge_sight_points",
    "grip_params",
    "grip_shift",
    "hand_grip_error",
    "hand_grips",
    "hand_params",
    "hand_shift",
    "head_camera",
    "host_cockpit",
    "host_interior",
    "load_cockpit",
    "main",
    "needle_check",
    "needle_gauge_ids",
    "patch_hierarchy",
    "patch_seats",
    "patch_vo3d",
    "place_pod",
    "pod_display",
    "promote_lod",
    "quat_from_to",
    "quat_rotate",
    "rim_grips",
    "seat_anchor",
    "seat_for_marker",
    "seat_markers",
    "section_points",
    "shell_blocks_view",
    "shell_closes",
    "shell_lids_cockpit",
    "uv_map_stats",
    "virtual_gauges",
    "visible_triangles",
    "wheel_display_pod",
    "with_display_faces",
]
