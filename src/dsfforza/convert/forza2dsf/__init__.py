"""Put a Forza Horizon 1 car's geometry into a DSF vehicle slot (version 1).

Takes an archive that already holds a host vehicle at --id (made with
src/dsfforza/build/clone_vehicle.py; the host's hierarchy, handling and sound stay) and
replaces the geometry of its full (N) and clean exterior (N+2000) entries with
the Forza car's main carbin. The interior (N+1000) is left alone unless
--cockpit: then it is rebuilt from <car>_cockpit.carbin, and the steering
hinge, gauges and head camera move to the Forza cockpit (src/dsfforza/convert/cockpit/,
research/cockpit.md).

Version 1 limits: no textures unless --textures (then lamps, badges and
interior sample the car's converted atlas through the host's Chassis/Lights
materials, see src/dsfforza/convert/dsf_materials.py; everything else uses one of DSF's
shared materials by role), no separate damage parts (all body geometry goes into
MODL part 0; other parts are emptied), Forza's wheel blur sections dropped (a vanilla-style
blur disc instead, wheel_blur_section). Wheels are moved
by rewriting the hub positions in the host's UPVH.

    uv run dsff forza2dsf out/dngvehicles.clone.sp out/forza.sp --id 304 \\
        --car out/fh1/NIS_370Z_10 --name NIS_370Z_10

FM4 car folders work the same way (lower-case file names are matched
case-insensitively). Wheel placement comes from Physics/MAXData.xml, or with
--gamedb from gamedb.slt Data_CarBody (car_dims).

As a library: load_car(folder, name), car_dims(car), then
convert(stream, full_vehc, exterior_vehc, car, dims, ConvertOptions(...))
edits the two VEHC entries in memory (src/dsfforza/build/port_car.py uses this).

See research/vehicle-model.md (DSF side) and src/dsfforza/formats/carbin.py (Forza side).
"""

from __future__ import annotations

from dsfforza.convert.forza2dsf.builder import (
    SHARED,
    SHARED_EXTERIOR,
    Builder,
    NewSub,
)
from dsfforza.convert.forza2dsf.car import (
    WHEEL_CARBIN,
    ForzaCar,
    cached_gamedb,
    car_dims,
    find_file,
    load_car,
    load_rotors,
)
from dsfforza.convert.forza2dsf.cli import (
    main,
)
from dsfforza.convert.forza2dsf.conversion import (
    ConvertOptions,
    convert,
    parse_lods,
)
from dsfforza.convert.forza2dsf.entry import (
    add_modl_part,
    fit_wheel_collision,
    host_wheels,
    modl_part_lods,
    rebuild_drsp,
    rebuild_modl,
    settle_wheels,
    split_rear_wheel,
)
from dsfforza.convert.forza2dsf.geometry import (
    add_geometry,
)
from dsfforza.convert.forza2dsf.materials import (
    CHROME,
    CHROME_GREYS,
    CHROME_LOD1,
    CHROME_UV,
    GLASS,
    MATERIAL_LAYOUT,
    PAINT,
    POSITION_SCALE,
    TRIM,
    TRIM_LOD1,
    TYRE,
    TYRE_LOD1,
    chrome_grey,
    raised_lamp,
    vanilla_sections,
)
from dsfforza.convert.forza2dsf.vertices import (
    INTERIOR_GLASS_RECT,
    Placement,
    spherical,
    vertex_sky,
)
from dsfforza.convert.forza2dsf.wheels import (
    BRAKE_ROTOR_MAX_TRIANGLES,
    ROTOR_CARBINS,
    ROTOR_DISC,
    ROTOR_HUB,
    SPOKE_DISC,
    SPOKE_DISC_LOD1,
    WHEEL_AO_RAYS,
    WHEEL_AO_SKIP,
    WHEEL_AO_VOXEL,
    WHEEL_BLUR_LIFT,
    WHEEL_BLUR_SECTION,
    WHEEL_BLUR_SEGMENTS,
    WHEEL_BLUR_SKY,
    WHEEL_SKY_OPEN,
    WHEEL_SKY_SIDE,
    WHEEL_UV,
    _hemisphere,
    _winding,
    axle_fits,
    blur_picture_map,
    brake_rotor_section,
    is_blur_disc,
    is_rotor,
    wheel_blur_section,
    wheel_has_rim,
    wheel_lod,
    wheel_material,
    wheel_normal,
    wheel_point,
    wheel_role,
    wheel_skies,
    wheel_sky,
)

__all__ = [
    "BRAKE_ROTOR_MAX_TRIANGLES",
    "CHROME",
    "CHROME_GREYS",
    "CHROME_LOD1",
    "CHROME_UV",
    "GLASS",
    "INTERIOR_GLASS_RECT",
    "MATERIAL_LAYOUT",
    "PAINT",
    "POSITION_SCALE",
    "ROTOR_CARBINS",
    "ROTOR_DISC",
    "ROTOR_HUB",
    "SHARED",
    "SHARED_EXTERIOR",
    "SPOKE_DISC",
    "SPOKE_DISC_LOD1",
    "TRIM",
    "TRIM_LOD1",
    "TYRE",
    "TYRE_LOD1",
    "WHEEL_AO_RAYS",
    "WHEEL_AO_SKIP",
    "WHEEL_AO_VOXEL",
    "WHEEL_BLUR_LIFT",
    "WHEEL_BLUR_SECTION",
    "WHEEL_BLUR_SEGMENTS",
    "WHEEL_BLUR_SKY",
    "WHEEL_CARBIN",
    "WHEEL_SKY_OPEN",
    "WHEEL_SKY_SIDE",
    "WHEEL_UV",
    "Builder",
    "ConvertOptions",
    "ForzaCar",
    "NewSub",
    "Placement",
    "_hemisphere",
    "_winding",
    "add_geometry",
    "add_modl_part",
    "axle_fits",
    "blur_picture_map",
    "brake_rotor_section",
    "cached_gamedb",
    "car_dims",
    "chrome_grey",
    "convert",
    "find_file",
    "fit_wheel_collision",
    "host_wheels",
    "is_blur_disc",
    "is_rotor",
    "load_car",
    "load_rotors",
    "main",
    "modl_part_lods",
    "parse_lods",
    "raised_lamp",
    "rebuild_drsp",
    "rebuild_modl",
    "settle_wheels",
    "spherical",
    "split_rear_wheel",
    "vanilla_sections",
    "vertex_sky",
    "wheel_blur_section",
    "wheel_has_rim",
    "wheel_lod",
    "wheel_material",
    "wheel_normal",
    "wheel_point",
    "wheel_role",
    "wheel_skies",
    "wheel_sky",
]
