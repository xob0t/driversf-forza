"""One conversion: ConvertOptions and convert(), which rebuilds a car's full, exterior and
interior entries in memory."""

from __future__ import annotations

import os
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.convert.forza2dsf.builder import SHARED_EXTERIOR, Builder, NewSub
from dsfforza.convert.forza2dsf.car import CarDims, ForzaCar
from dsfforza.convert.forza2dsf.entry import (
    REAR_PART_RADIUS,
    REAR_PART_WIDTH,
    HostWheels,
    fill_empty_parts,
    fit_wheel_collision,
    host_wheels,
    lights_template,
    patch_hubs,
    rebuild_drsp,
    rebuild_modl,
    split_rear_wheel,
)
from dsfforza.convert.forza2dsf.geometry import add_geometry
from dsfforza.convert.forza2dsf.materials import (
    DEFAULT_LODS,
    DEFAULT_SHADOW_LOD,
    EXTERIOR_MASK,
    MATERIAL_LAYOUT,
    NONR_LIMIT,
    PAINT_UV,
    SHADOW_MASK,
)
from dsfforza.convert.forza2dsf.vertices import Placement, host_glass_links, interior_glass_skin
from dsfforza.convert.forza2dsf.wheels import (
    axle_fits,
    axle_tyres,
    blur_picture_map,
    brake_rotor_section,
    wheel_blur_section,
    wheel_lod,
)
from dsfforza.formats.chnk import Entry, entry_size
from dsfforza.formats.vehicle_mesh import child, payload

# --- one conversion -----------------------------------------------------------------


@dataclass
class ConvertOptions:
    lods: dict[int, int] = field(default_factory=lambda: parse_lods(DEFAULT_LODS))
    shadow_lod: int = DEFAULT_SHADOW_LOD
    exterior_lod: int = 1
    host_axle_mid: float | None = None  # None: from the host's wheel table (Desanne 0.1155)
    skip_full: bool = False
    skip_exterior: bool = False
    # Only the full entry, as the whole conversion leaves it: no exterior entry and no
    # interior conversion (the cockpit's edits of the full entry's UPVH and VO3D stay), for
    # a size check before the rest is converted (port_car's LOD retries).
    full_only: bool = False
    no_hubs: bool = False
    keep_host_materials: bool = False
    fill_empty_parts: bool = False
    keep_host_geometry: bool = False
    no_wheel: bool = False
    # A host with one wheel part for both axles gets a rear wheel part (split_rear_wheel) when
    # the rear tyre differs from the front (REAR_PART_RADIUS, REAR_PART_WIDTH).
    split_rear_wheel: bool = True
    empty_other_parts: bool = False
    textures: str | None = None  # None, "replace" or "clone"
    atlas_size: tuple[int, int] | None = None
    swatch_trims: bool = False
    merge_submodels: bool = False
    uid_seed: bytes | None = None  # 4 bytes; random when None
    # Interior entry (N+1000) from <car>_cockpit.carbin, steering wheel hinge,
    # gauges, mirrors and head camera moved to the Forza cockpit (src/dsfforza/convert/cockpit/).
    cockpit: bool = False
    cockpit_opts: object = None  # cockpit.CockpitOptions; defaults when None
    # Why the host is the same vehicle as the Forza car (host_select.same_vehicle),
    # or None. When Forza's cockpit carbin is a placeholder (cockpit.is_stub), such a
    # host keeps its own vanilla interior (cockpit, gauges, wheel, hands, head camera)
    # instead of the main carbin's low-detail cabin (research/cockpit.md).
    same_vehicle_host: str | None = None
    gamedbs: list = field(default_factory=list)  # for CameraOverrides (eye point)
    # Bind windscreen/rear-window glass to the host's glass-panel records like
    # vanilla glass (host_glass_links) instead of the chassis. Off by default
    # until tested in game.
    glass_links: bool = False
    # Working lamps (src/dsfforza/convert/lights.py): lamp triangles carry their light in vertex
    # semantic 9 byte 2, and the host's UPVH light table is moved onto them.
    lights: bool = True
    # Factory paint palette (src/dsfforza/convert/paint.py): the host's SPSR colours replaced by
    # the car's gamedb.slt Combo_Colors (gamedb from gamedbs, else next to the car).
    paint: bool = True
    # Vanilla-like damage (src/dsfforza/convert/damage/): body triangles on the host's panels
    # (semantic 9), crush targets (semantic 12), per-panel MODL parts, panel
    # hinges/bodies/collision moved onto the Forza panels. Off until tested in game.
    damage: bool = False
    damage_opts: object = None  # damage.DamageOptions; defaults when None
    # Licence plates (src/dsfforza/convert/plates.py): the vanilla DSF plate (shared paint atlas)
    # on the Forza car's plate recess / mounting face, at each end the host has
    # a plate, on the panel under it. Forza `numberplate` quads are replaced.
    plates: bool = True
    # Exhaust markers (src/dsfforza/convert/exhaust.py): the host's role-0x4D markers (smoke,
    # backfire, the exhaust sound) moved onto the Forza tailpipes, one per tip, up to 4.
    exhaust: bool = True
    # Wheel speed blur (wheel_blur_section): a disc over each wheel's rim face that fades in as
    # the wheel spins, like vanilla wheels' spoke discs.
    wheel_blur: bool = True
    # Brake rotors (brake_rotor_section): Forza's front and rear rotor in the wheel parts, behind
    # the spokes, at the finest wheel LODs.
    brake_rotors: bool = True
    # The archive's shared GLOBAL_Wheel_Blur_SpokeDisc MACOs ({uid: Entry},
    # dsf_materials.shared_macos): with them and --textures the disc draws Forza's own
    # spinning-wheel picture; without, the shared disc and its texture.
    blur_materials: dict | None = None


def parse_lods(text: str) -> dict[int, int]:
    return {int(m): int(lod) for m, lod in (pair.split(":") for pair in text.split(","))}


def convert(
    stream,
    full: Entry,
    exterior: Entry,
    car: ForzaCar,
    dims: CarDims,
    opts: ConvertOptions,
    log=print,
    interior: Entry | None = None,
) -> dict:
    """Replace the geometry of a host vehicle's full (kind 0) and clean exterior
    (kind 2) VEHC entries, in memory, with the Forza car. The entries are edited
    in place; leaves read from `stream` unless already edited. With
    opts.cockpit and the interior (kind 1) entry, the interior is rebuilt from
    the car's cockpit carbin too. Returns a report."""

    # The turn maps Forza z to -z, so the wheelbase midpoint (Forza z = mid_z, see
    # CarDims) lands at -mid_z + dz = axle_mid.
    # Forza ground (y = -ride_height) goes to the host's ground (hub y - tyre radius).
    # Hub height, axle midpoint and wheel parts are the host's own (host_wheels).
    def read_wheels() -> HostWheels:
        return host_wheels(
            payload(stream, child(child(full, "NONR"), "UPVH")),
            payload(stream, child(child(full, "NONR"), "MODL")),
            payload(stream, child(child(exterior, "NONR"), "MODL")),
        )

    hw = read_wheels()
    seed = opts.uid_seed or os.urandom(4)
    # Per-axle wheel size. Forza draws its one wheel model at each axle's Data_Car tyre size
    # (research/wheels.md, "Wheel clearance"): the model matches the front tyre on most cars
    # (370Z, Veyron SS, M3) but not on 160 of 573 (FER_F151_11's is 0.393 m against a 0.340 m
    # tyre, SUZ_Liana_02's 0.248 against 0.298; Forza's thumbnails show the tyre size). So the
    # front wheels are scaled to the front tyre and the rear wheel part(s) to the rear tyre.
    # A host with one wheel part for both axles gets a rear copy of it first when the tyres
    # differ (split_rear_wheel; the Jaguar XKR's rear tyre is 42 mm taller than its front).
    # The hubs stand the front tyres on Forza's ground; the rear hubs go to the rear track and
    # up or down by the radius difference, so all four tyres stand on the same ground.
    tyres = axle_tyres(car.name, opts.gamedbs)
    fits = axle_fits(dims.radius, dims.tyre_width, tyres)
    rear_split = None
    if (
        opts.split_rear_wheel
        and not opts.no_wheel
        and not any(axle == "rear" for axle in hw.axles.values())
        and (
            abs(dims.radius * (fits["rear"][1] - fits["front"][1])) > REAR_PART_RADIUS
            or abs(dims.tyre_width * (fits["rear"][0] - fits["front"][0])) > REAR_PART_WIDTH
        )
    ):
        rear_split = split_rear_wheel(stream, full, exterior, seed[:3] + bytes([seed[3] ^ 0x66]))
        if rear_split is not None:
            hw = read_wheels()
            log(
                f"rear wheel part: the host's shared wheel part copied for the rear wheels "
                f"(full {rear_split['full'][0]} -> {rear_split['full'][1]}, exterior "
                f"{rear_split['exterior'][0]} -> {rear_split['exterior'][1]})"
            )
    # The ground the engine counts camera heights from stays the host's as shipped.
    from dsfforza.convert import vehicle_cameras

    camera_ground = vehicle_cameras.host_ground(
        payload(stream, child(child(full, "NONR"), "UPVH")), payload(stream, child(child(full, "NONR"), "VO3D"))
    )
    axle_mid = hw.axle_mid if opts.host_axle_mid is None else opts.host_axle_mid
    # With one shared part (tyres alike, or no copy) the rear wheels are front-sized.
    split = any(axle == "rear" for axle in hw.axles.values())
    rear_fit = fits["rear"] if split else fits["front"]
    fits["both"] = fits["front"]
    front_radius, front_width = dims.radius * fits["front"][1], dims.tyre_width * fits["front"][0]
    place = Placement(dy=hw.hub_y - front_radius + dims.ride_height, dz=axle_mid + dims.mid_z)

    def part_fit(kind: int, part: int) -> tuple[float, ...]:
        # with the tyre's centre: some wheel models sit off their origin (TOY_6_Supra_05)
        return (*fits[hw.axles.get((kind, part), "both")], *dims.tyre_centre)

    rotor_cache: dict = {}

    def wheel_sections(kind: int, part: int, lod: int, finest: bool = True) -> list:
        """car.wheel, plus the axle's brake rotor (brake_rotor_section) on the entry's finest
        LOD. One rotor section per (axle, fit, LOD), so entries drawing the same LOD share
        wheel_skies."""
        axle = "rear" if hw.axles.get((kind, part)) == "rear" else "front"
        rotor = car.rotors.get(axle) if opts.brake_rotors else None
        if not rotor or not finest:
            return car.wheel
        fit = part_fit(kind, part)
        key = (axle, fit, wheel_lod(car.wheel, lod))
        if key not in rotor_cache:
            rotor_cache[key] = section = brake_rotor_section(rotor, key[2], fit)
            if section is not None:
                tris = sum(len(sub.triangles()) for sub in section.subsections)
                report.setdefault("brake_rotors", {})[f"{axle} LOD {key[2]}"] = tris
                log(f"brake rotor ({axle}, LOD {key[2]}): {tris} triangles{' (plain disc)' if section.plain else ''}")
        return car.wheel if rotor_cache[key] is None else [*car.wheel, rotor_cache[key]]

    half_track = (dims.track_outer - front_width) / 2
    rear_half_track = ((dims.rear_track_outer or dims.track_outer) - dims.tyre_width * rear_fit[0]) / 2
    rear_hub_y = hw.hub_y + dims.radius * rear_fit[1] - front_radius
    log(
        f"{car.name}: {len(car.body)} vanilla body sections, wheelbase {dims.wheelbase:.3f}, "
        f"tyre radius {front_radius:.3f} (model {dims.radius:.3f}), track {2 * half_track:.3f} ({dims.source})"
    )
    swapped = {lod: wheel_lod(car.wheel, lod) for lod in [*opts.lods.values(), opts.exterior_lod]}
    if any(a != b for a, b in swapped.items()):
        log(
            "wheel LODs without a tyre, using the nearest that has one: "
            + ", ".join(f"{a} -> {b}" for a, b in sorted(swapped.items()) if a != b)
        )
    blur_disc = None
    if opts.wheel_blur and not opts.no_wheel:
        import dataclasses

        blur_disc = wheel_blur_section(car.wheel, set(swapped.values()), dims.tyre_centre)
        if blur_disc is not None:
            car = dataclasses.replace(car, wheel=[*car.wheel, blur_disc])
            log(
                "wheel blur disc: "
                + ", ".join(
                    f"LOD {sub.lod} {'always (no sharp rim)' if sub.always else 'fades in'}"
                    for sub in blur_disc.subsections
                )
                + ("" if blur_picture_map(car.wheel) else "; no blur_rim to map Forza's picture: the vanilla map")
            )
    report: dict = {
        "placement": {
            "source": dims.source,
            "wheelbase": round(dims.wheelbase, 4),
            "track": round(2 * half_track, 4),
            "track_outer": round(dims.track_outer, 4),
            "tyre_radius": round(dims.radius, 4),  # the wheel model's
            "tyre_width": round(dims.tyre_width, 4),
            "tyre_centre": [round(c, 4) for c in dims.tyre_centre],
            "front_fit": [round(c, 4) for c in fits["front"]],
            "ground_radius": round(front_radius, 4),  # the hubs stand this far over Forza's ground
            "ride_height": round(dims.ride_height, 4),
            "mid_z": round(dims.mid_z, 4),
            "dy": round(place.dy, 4),
            "dz": round(place.dz, 4),
            "host_hub_y": round(hw.hub_y, 4),
            "host_axle_mid": round(axle_mid, 4),
            "host_wheel_parts": {"full": hw.full_parts, "exterior": hw.exterior_parts},
            "rear_wheel_part": rear_split,  # split_rear_wheel's copy, or None
            "rear_fit": [round(c, 4) for c in rear_fit],
            "rear_half_track": round(rear_half_track, 4),
            "rear_hub_y": round(rear_hub_y, 4),
            "tyres": tyres,
        }
    }

    tex_full = tex_ext = None
    layout = dict(MATERIAL_LAYOUT)  # this conversion's materials (no state shared between cars)
    if opts.textures:
        import dataclasses

        from dsfforza.convert import cutout, dsf_materials, trim_art
        from dsfforza.convert import forza_materials as fm

        lods = set(opts.lods.values()) | {opts.shadow_lod, opts.exterior_lod}
        # Alpha-tested badges as geometry: DSF's Chassis material has no alpha test,
        # so a badge quad drew its whole rectangle (src/dsfforza/convert/cutout.py).
        body, cut = cutout.cut_alpha_tested(car.body, fm.AtlasCache(car.folder), lods=lods)
        if cut:
            car = dataclasses.replace(car, body=body)
            report["cutout"] = {k: {**v, "area": round(v["area"], 5)} for k, v in cut.items()}
            log(
                "alpha-tested decals cut to their mask: "
                + ", ".join(f"{k} {v['cut']} -> {v['made']}" for k, v in sorted(cut.items()))
            )
        # Trim triangles on light artwork (the Alfa GTA's quadrifoglio badges) and dark
        # detail (the Cougar Eliminator's grille bars) to the atlas, flat mid-grey ones to
        # a colour cell, bare metal (the Veyron's scoops) to the shared chrome: the shared
        # trim draws them all as one dark texel (src/dsfforza/convert/trim_art.py).
        body, art = trim_art.split_trim_art(car.body, fm.AtlasCache(car.folder), lods=lods)
        if art:
            car = dataclasses.replace(car, body=body)
            report["trim_art"] = {k: {**v, "area": round(v["area"], 5)} for k, v in art.items()}
            log(
                "trim artwork off the shared trim (metal/detail/grey/gloss): "
                + ", ".join(
                    f"{k} {v['triangles']} ({v['metal']}/{v['detail']}/{v['grey']}/{v['gloss']}, {v['area']:.3f} m2)"
                    for k, v in sorted(art.items())
                )
            )
        wheels = None
        if not opts.no_wheel:  # the wheel LODs add_geometry will draw (wheel_lod)
            wheels = (car.wheel, {wheel_lod(car.wheel, lod) for lod in [*opts.lods.values(), opts.exterior_lod]})
        tex_full, tex_ext = dsf_materials.prepare(
            stream,
            full,
            exterior,
            car.folder,
            car.body,
            lods,
            mode=opts.textures,
            size=opts.atlas_size,
            swatch_trims=opts.swatch_trims,
            log=log,
            interior_vehc=interior,
            uid_seed=seed,
            wheels=wheels,
            blur_materials=opts.blur_materials,
        )
        layout.update(tex_full.layout)
        layout.update(tex_ext.layout)
        if tex_full.texturing.lens_report:
            report["lenses"] = tex_full.texturing.lens_report

    # Cockpit: fit the Forza cockpit and find the host's steering-wheel node first,
    # so the body's steering wheel can be skinned to it as well.
    cf = host_cp = None
    skin: dict[str, bytes] = {}

    def align_steering_wheel(target, onto: str) -> bool:
        """The main carbin's steering wheel (the chase views') moved, turned and scaled
        onto `target` (cockpit.align_main_wheel); False when it can't be fitted."""
        import dataclasses

        from dsfforza.convert import cockpit

        nonlocal car
        body, moved = [], False
        for section in car.body:
            if section.name == "steering_wheel":
                aligned, wheel_report = cockpit.align_main_wheel(section, target)
                report["main_steering_wheel"] = {**wheel_report, "onto": onto}
                log(
                    f"main-carbin steering wheel: moved {wheel_report.get('move')} m, turned "
                    f"{wheel_report.get('turn_deg')} deg, scaled {wheel_report.get('scale')} onto the "
                    f"{onto}" + (f" ({wheel_report['kept']}: kept)" if aligned is None else "")
                )
                moved = aligned is not None
                section = aligned or section
            body.append(section)
        car = dataclasses.replace(car, body=body)
        return moved

    if opts.cockpit:
        from dsfforza.convert import cockpit

        # Without a cockpit carbin, fit() builds the cabin from the main carbin.
        sections = cockpit.load_cockpit(car.folder, car.name) or []
        if interior is None or opts.skip_full:
            log("cockpit: needs the interior entry and the full entry; skipped")
        else:
            # The main carbin stands in for stub cockpit carbins and missing steering wheels.
            try:
                cf = cockpit.fit(sections, car.name, opts.gamedbs, folder=car.folder, main_sections=car.sections)
            except ValueError as error:
                log(f"cockpit: {error}; the interior stays the host's")
            else:
                if cf.source != "cockpit":
                    log(f"cockpit: {cf.source}")
                if not cf.lod0_atlases and opts.same_vehicle_host:
                    # Forza ships a placeholder cockpit; the host's vanilla interior is
                    # this vehicle's (same model or badge twin) and complete.
                    log(
                        f"cockpit: placeholder Forza cockpit and the host is the same vehicle "
                        f"({opts.same_vehicle_host}): the interior stays the host's"
                    )
                    report["cockpit_host"] = {"forza": cf.source, "same_vehicle": opts.same_vehicle_host}
                    cf = None
                    # The chase views draw the main carbin's wheel and the host's vanilla
                    # rig turns it and holds it: it goes onto the host interior's own wheel,
                    # skinned to the host's steering record (issue #86).
                    from dsfforza.convert.damage import load_mesh

                    try:
                        wheel_host = cockpit.host_cockpit(payload(stream, child(child(full, "NONR"), "UPVH")))
                    except ValueError as error:
                        log(f"main-carbin steering wheel: {error}; kept where Forza has it")
                    else:
                        target = cockpit.host_wheel(load_mesh(stream, interior), wheel_host.wheel_skin, place.forza)
                        if target is None:
                            log("main-carbin steering wheel: the host's interior has no wheel; kept where Forza has it")
                        elif align_steering_wheel(target, "host interior's"):
                            skin = {"steering_wheel": wheel_host.wheel_skin}
                else:
                    host_cp = cockpit.host_cockpit(payload(stream, child(child(full, "NONR"), "UPVH")))
                    skin = {"steering_wheel": host_cp.wheel_skin}
                    # The hands grip the cockpit's wheel in every view; the chase views draw
                    # the main carbin's, which Forza models elsewhere (up to 17 cm away): it
                    # goes onto the cockpit wheel, at its size (dsfforza.asi [hands] moves the
                    # chase views' driver onto that rim too).
                    align_steering_wheel(cf.wheel, "cockpit's")

    links = host_glass_links(stream, full) if opts.glass_links else None
    if links is not None:
        log(f"glass links: { ({k: tuple(v) for k, v in links.items()}) }")
        report["glass_links"] = {k: list(v) for k, v in links.items()}

    if opts.plates and car.body:
        # Read the host's plates before its geometry is replaced; the plates
        # join the body as synthetic sections, so damage, LODs and the shadow
        # treat them like Forza geometry.
        import dataclasses

        from dsfforza.convert import plates

        host = plates.host_plates(stream, full)
        body, plate_plan = plates.apply(car, place, hw.hub_y - front_radius, host, log=log)
        car = dataclasses.replace(car, body=body)
        report["plates"] = plate_plan.report

    if opts.damage and car.body:
        # Bonnet/boot undersides and the trims under the bonnet into their own
        # subsections (before the lamp plan, which keys on subsections), so
        # DamagePlan can texture what a missing bonnet exposes like vanilla.
        import dataclasses

        from dsfforza.convert import damage

        engine = damage.engine_placement(car.name, opts.gamedbs, car.folder)
        body, exposed = damage.split_exposed(car.body, place, log=log, engine=engine)
        car = dataclasses.replace(car, body=body)
        report["exposed"] = exposed

    lamps = None
    if opts.lights:
        from dsfforza.convert import lights

        lenses = tex_full.texturing.lens_subs if tex_full is not None else frozenset()
        # Lens colours from Forza's own texels: the atlas copy the texturing drew from has
        # the lamps behind the lenses baked into them (src/dsfforza/convert/lenses.py).
        texel = seen = None
        if tex_full is not None:
            from dsfforza.convert import forza_materials as fm

            texel = lights.texel_sampler(fm.AtlasCache(car.folder))
            # What the car is drawn with (lenses baked): red rear texels light with red lamps.
            seen = lights.seen_sampler(tex_full.texturing.atlases)
        lamps = lights.LampPlan.build(car.body, place, axle_mid, lenses, texel, seen).assign(
            payload(stream, child(child(full, "NONR"), "UPVH"))
        )
        report["lights"] = lamps.report()
        if tex_full is not None:
            t0 = time.perf_counter()
            if tex_full.set_rear_lamps(stream, full, car.body, lamps.rear_tail_lamp):
                log(
                    f"lights: rear tail lamps ranked per planned lamp, atlas rendered again ({time.perf_counter() - t0:.2f} s)"
                )
            # Head lamps glow in the colour their lenses pass (a selective-yellow lens: yellow).
            tinted = lamps.tint_heads(tex_full.texturing.head_filters(car.body, lamps.front_head_lamp))
            if tinted:
                report["lights"] = lamps.report()
                log(
                    "lights: head lamp colour through their lenses: "
                    + ", ".join(f"{'/'.join(map(str, k[:2]))}#{k[3]} {a} -> {b}" for k, (a, b) in tinted.items())
                )
        log(f"lights: {len(lamps.lamps)} lamps on {len(lamps.slots)} host light entries, {len(lamps.unlit)} unlit")

    dmg = None
    if opts.damage:
        from dsfforza.convert import damage

        dmg = damage.DamagePlan(
            stream,
            full,
            exterior,
            car,
            place,
            opts.damage_opts,
            log=log,
            interior=interior,
            engine=damage.engine_placement(car.name, opts.gamedbs, car.folder),
        )
        report["damage"] = dmg.report

    if not opts.skip_full:
        # Full entry: part 0 gets the body per LOD, the wheel part gets the wheel.
        builder = Builder(seed, layout=layout)
        part_lods: dict[int, dict[int, list[NewSub]]] = {0: {}}
        for mask, lod in opts.lods.items():
            if dmg is not None:  # per-panel parts plus the merged part 0
                dmg.full_lod(
                    builder, part_lods, mask, lod, textures=tex_full, merge=opts.merge_submodels, skin=skin, lamps=lamps
                )
            else:
                part_lods[0][mask] = add_geometry(
                    builder,
                    car.body,
                    lod,
                    place,
                    wheel=False,
                    textures=tex_full,
                    merge=opts.merge_submodels,
                    skin=skin,
                    glass_links=links,
                    lamps=lamps,
                )
            if not opts.no_wheel:
                for wheel_part in hw.full_parts:  # Desanne: part 31; 33 hosts split front/rear
                    part_lods.setdefault(wheel_part, {})[mask] = add_geometry(
                        builder,
                        wheel_sections(0, wheel_part, lod, finest=mask == min(opts.lods)),
                        lod,
                        place,
                        wheel=True,
                        wheel_fit=part_fit(0, wheel_part),
                        textures=tex_full,
                    )
        part_lods[0][SHADOW_MASK] = add_geometry(
            builder,
            car.body,
            opts.shadow_lod,
            place,
            wheel=False,
            textures=tex_full,
            merge=opts.merge_submodels,
            skin=skin,
            glass_links=links,
            lamps=lamps,
        )
        nonr = child(full, "NONR")
        modl = child(nonr, "MODL")
        host_modl = payload(stream, modl)
        if opts.fill_empty_parts:
            log(f"filled {fill_empty_parts(builder, host_modl, part_lods)} emptied part LODs")
        rebuild_drsp(
            stream,
            child(child(full, "MDXN"), "DRSP"),
            builder,
            keep_host_materials=opts.keep_host_materials or bool(opts.textures),
            keep_host_geometry=opts.keep_host_geometry,
        )
        modl.payload = rebuild_modl(
            host_modl, part_lods, keep_other_parts=opts.keep_host_geometry and not opts.empty_other_parts
        )
        hubs = 0
        if not opts.no_hubs:
            upvh = child(nonr, "UPVH")
            upvh.payload, hubs = patch_hubs(
                payload(stream, upvh),
                axle_mid + dims.wheelbase / 2,
                axle_mid - dims.wheelbase / 2,
                half_track,
                hw.hub_y,
                rear_half_track,
                rear_hub_y,
            )
            if not opts.no_wheel:
                rear_size = (dims.radius * rear_fit[1], dims.tyre_width * rear_fit[0])
                upvh.payload, sized = fit_wheel_collision(upvh.payload, (front_radius, front_width), rear_size)
                log(
                    f"wheel collision cylinders: {sized} sized to the tyres (front r {front_radius:.3f} "
                    f"w {front_width:.3f}, rear r {rear_size[0]:.3f} w {rear_size[1]:.3f})"
                )
        if lamps is not None:
            upvh = child(nonr, "UPVH")
            upvh.payload = lamps.patch_upvh(payload(stream, upvh))
        if dmg is not None:
            upvh = child(nonr, "UPVH")
            upvh.payload, _ = dmg.patch_upvh(payload(stream, upvh))
        vertex_bytes = sum(len(b) for b in builder.buffers.values())
        log(
            f"full entry: {len(builder.subs)} submodels, {vertex_bytes // 1024} KiB vertices, "
            f"{len(builder.indices)} indices; {hubs} hub positions patched"
        )
        report["full"] = {
            "submodels": len(builder.subs),
            "vertex_bytes": vertex_bytes,
            "indices": len(builder.indices),
            "hubs_patched": hubs,
        }

    if not opts.skip_exterior and not opts.full_only:
        # Clean exterior entry: body in part 0, the wheel in the part(s) the wheel records'
        # LOD links name (the last part on the Desanne).
        ext_builder = Builder(seed[:3] + bytes([seed[3] ^ 0xFF]), layout=layout)
        ext_modl_entry = child(child(exterior, "NONR"), "MODL")
        ext_template = payload(stream, ext_modl_entry)
        ext_parts = struct.unpack_from("<I", ext_template)[0]
        if dmg is not None:
            ext_body = dmg.exterior(
                ext_builder,
                opts.exterior_lod,
                textures=tex_ext,
                merge=opts.merge_submodels,
                skin=skin,
                lamps=lamps,
                shared=SHARED_EXTERIOR,
            )
        else:
            ext_body = add_geometry(
                ext_builder,
                car.body,
                opts.exterior_lod,
                place,
                wheel=False,
                textures=tex_ext,
                merge=opts.merge_submodels,
                skin=skin,
                glass_links=links,
                lamps=lamps,
                shared=SHARED_EXTERIOR,
            )
        ext_lods = {0: {EXTERIOR_MASK: ext_body}}
        for wheel_part in hw.exterior_parts or [ext_parts - 1]:
            ext_lods[wheel_part] = {
                EXTERIOR_MASK: add_geometry(
                    ext_builder,
                    wheel_sections(2, wheel_part, opts.exterior_lod),
                    opts.exterior_lod,
                    place,
                    wheel=True,
                    wheel_fit=part_fit(2, wheel_part),
                    shared=SHARED_EXTERIOR,
                    textures=tex_ext,
                )
            }
        rebuild_drsp(
            stream, child(child(exterior, "MDXN"), "DRSP"), ext_builder, keep_host_materials=bool(opts.textures)
        )
        ext_modl_entry.payload = rebuild_modl(ext_template, ext_lods)
        log(f"exterior entry: {len(ext_builder.subs)} submodels")
        report["exterior"] = {"submodels": len(ext_builder.subs)}

    if cf is not None:
        from dsfforza.convert import cockpit

        nonr = child(full, "NONR")
        upvh = child(nonr, "UPVH")
        host_upvh = payload(stream, upvh)
        # The engine turns a needle only through a gauge record for its id: give a
        # one-gauge host (speedometer only, or the MP4-12C's tachometer only) the other.
        need = {cockpit.GAUGE_IDS[f] for f in cf.gauges if f in cockpit.GAUGE_IDS}
        host_upvh, gauges_added = cockpit.add_gauge_records(host_upvh, need)
        if gauges_added:
            host_cp = cockpit.host_cockpit(host_upvh)
            log(f"cockpit: gauge records added for ids {gauges_added} (the host has no entry for them)")
        unturned = cockpit.drop_unturned(cf, host_cp.gauge_slots)
        if unturned:
            log(f"cockpit: no {', '.join(unturned)} needle: the host has no gauge record to turn it")
        upvh.payload, hier = cockpit.patch_hierarchy(host_upvh, host_cp, place, cf)
        if gauges_added:
            hier["gauges_added"] = gauges_added
        eye = place.point(cf.eye)
        # The driver model sits on the seat marker (role 10) and its hands hold the wheel
        # only where vanilla cars have it from the seat: the seat goes where its grips
        # land on Forza's rim (steering.seat_anchor, cockpit.seat_anchor).
        upvh.payload, hier["seats"] = cockpit.patch_seats(upvh.payload, eye, hier["steering"]["seat_anchor"])
        # The raise stops before the cluster's cowl hides the needles the Forza eye sees.
        drawn = [
            s for n, s in cf.static.items() if n not in cockpit.SKIP_SECTIONS and not cockpit.LIGHT_SECTIONS.search(n)
        ]
        sight = cockpit.GaugeSight(drawn, cockpit.gauge_sight_points(cf))
        camera_eye = cockpit.camera_eye(
            eye,
            [place.point(q) for q in cockpit.section_points(cf.static["steering_wheel"])],
            sight=lambda rise: sight((cf.eye[0], cf.eye[1] + rise, cf.eye[2])),
        )
        rise = camera_eye[1] - eye[1]
        gauge_sight = {
            "camera_rise": round(rise, 4),
            "forza_eye": round(sight(cf.eye), 3),
            "camera": round(sight((cf.eye[0], cf.eye[1] + rise, cf.eye[2])), 3),
        }
        camera = cockpit.camera_params(camera_eye, hier["steering"]["centre"][0])
        # The interior is culled from the final camera (Placement is its own inverse up to dy, dz).
        cf.camera = (-camera_eye[0], camera_eye[1] - place.dy, -(camera_eye[2] - place.dz))
        vo3d = child(nonr, "VO3D")
        # The speedometer stops at the Forza dial's last mark (p605).
        gauge_vo3d = cockpit.gauge_params(cf)
        # The hand rig's offset from the steering marker would move the wheel off its Forza place;
        # dsfforza.asi [hands] puts the hands on the rim the steering report describes.
        vo3d.payload = cockpit.patch_vo3d(
            payload(stream, vo3d), {**camera, **gauge_vo3d, **cockpit.hand_params(hier["steering"])}
        )
        if opts.full_only:  # the interior entry stays the host's (a size check of the full entry)
            interior_report = {"skipped": "full_only"}
        else:
            paint_uv = tex_ext.paint_uv if tex_ext is not None else PAINT_UV
            interior_report = cockpit.convert_interior(
                stream,
                interior,
                car,
                cf,
                place,
                seed,
                paint_uv,
                opts.cockpit_opts or cockpit.CockpitOptions(),
                host_cp.wheel_skin,
                log=log,
                glass_links=links,
                glass_skin=interior_glass_skin(dmg, car, opts),
                layout=layout,
                lights_template=lights_template(stream, full) if opts.textures else None,
            )
            if dmg is not None:  # the shell's paint samples the decal atlas at UV1 too
                dmg.interior_uv1(stream, interior)
            # The engine picks each needle's transform by semantic 8's gauge id; any
            # pass that rewrites it leaves the needles static.
            needle_ids = cockpit.needle_gauge_ids(stream, interior)
            # Digital displays (digital_dash) draw from the needle buffer with ids 1..7.
            allowed = set(cockpit.GAUGE_IDS.values()) | (
                set(range(1, 8)) if "digital_displays" in interior_report else set()
            )
            stray = sorted(k for k in needle_ids if k not in allowed)
            if stray:
                raise ValueError(
                    f"interior needle vertices with gauge ids {stray[:6]} (not in "
                    f"{sorted(cockpit.GAUGE_IDS.values())}): the needles would not turn"
                )
            interior_report["needle_ids"] = {str(int(k)): n for k, n in sorted(needle_ids.items())}
        log(
            f"cockpit: eye {tuple(round(c, 3) for c in eye)} (offset {cf.offset}, {cf.offset_source}); "
            f"wheel {hier['steering']['centre']}; VO3D 430..432 = "
            f"{', '.join(f'{v:.3f}' for v in camera.values())}"
        )
        report["cockpit"] = {
            "eye": [round(c, 4) for c in eye],
            "eye_forza": [round(c, 4) for c in cf.eye],
            "eye_offset": list(cf.offset),
            "eye_source": cf.offset_source,
            "vo3d": {str(k): round(v, 4) for k, v in {**camera, **gauge_vo3d}.items()},
            "gauge_sight": gauge_sight,
            "hierarchy": hier,
            "interior": interior_report,
        }
    if not opts.skip_full:
        # The Bonnet camera: in front of the Forza windscreen, where vanilla cars have it
        # relative to theirs (the host's sat inside lower cars' cabins).
        vo3d = child(child(full, "NONR"), "VO3D")
        host_cam = vehicle_cameras.bonnet_camera(payload(stream, vo3d), camera_ground)
        windscreen = vehicle_cameras.windscreen_centre(vehicle_cameras.forza_windscreen(car.body, place))
        cam_report = {"ground": round(camera_ground, 4), "host": [round(c, 4) for c in host_cam]}
        if windscreen is None:
            cam_report["kept"] = f"no {vehicle_cameras.WINDSCREEN_SECTION} section"
            log(f"bonnet camera: no {vehicle_cameras.WINDSCREEN_SECTION}; the host's stays")
        else:
            body = vehicle_cameras.section_triangles(car.body, place, opts.exterior_lod)
            cam, rays = vehicle_cameras.bonnet_position(windscreen, body)
            params = vehicle_cameras.bonnet_params(cam, camera_ground)
            vo3d.payload = vehicle_cameras.patch_vo3d(payload(stream, vo3d), params)
            cam_report.update(
                {
                    "windscreen": [round(c, 4) for c in windscreen],
                    "camera": [round(c, 4) for c in cam],
                    "rays": rays,
                    "vo3d": {str(k): round(v, 4) for k, v in params.items()},
                }
            )
            log(f"bonnet camera: {tuple(round(c, 3) for c in cam)} (host's {tuple(round(c, 3) for c in host_cam)})")
        report["bonnet_camera"] = cam_report
    if opts.exhaust and not opts.skip_full:
        # Smoke, backfire and the exhaust sound come out of the Forza tailpipes, not the
        # host's (src/dsfforza/convert/exhaust.py). Last of the UPVH edits: it can add or
        # drop marker records, which moves the record and marker indices after them.
        from dsfforza.convert import exhaust

        upvh = child(child(full, "NONR"), "UPVH")
        tips = exhaust.find_tips(car.body, place)
        # Without tips: the host body's box onto the Forza body's, as for the damage panels.
        boxes = (dmg.full_field.box, dmg.forza_box) if dmg is not None else None
        upvh.payload, report["exhaust"] = exhaust.patch_upvh(payload(stream, upvh), tips, boxes)
        log(exhaust.describe(report["exhaust"]))
    # Hosts with more than one chassis record (172 and 38 other vanilla cars) draw a
    # variant part instead of part 0: give those parts the body too (src/dsfforza/convert/chassis_variants.py).
    from dsfforza.convert import chassis_variants

    report["chassis_variants"] = chassis_variants.fill(
        stream,
        full,
        interior if cf is not None and not opts.full_only else None,
        None if opts.skip_exterior or opts.full_only else exterior,
        seed,
        log=log,
        full_rebuilt=not opts.skip_full,
    )
    if opts.paint and not opts.skip_full:
        from dsfforza.convert import paint

        gamedb = next((Path(g) for g in opts.gamedbs if g and Path(g).exists()), None) or paint.find_gamedb(car.folder)
        colours = paint.forza_colours(gamedb, car.name, car.folder) if gamedb else []
        # Leave room under the NONR buffer for handling edits made after this.
        report["paint"] = paint.install(stream, full, colours, nonr_limit=NONR_LIMIT[0] - 0x400)
        log(f"paint: {report['paint'].get('installed')} factory colours (host had {report['paint'].get('host_count')})")
    tint = tex_full.texturing.wheel_blur_tint if tex_full is not None else None
    if blur_disc is not None:
        report["wheel_blur"] = {
            "lods": {sub.lod: "always" if sub.always else "fade" for sub in blur_disc.subsections},
            "picture": tint is not None,
        }
    if tint is not None and not opts.skip_full:
        from dsfforza.convert import paint

        report["wheel_blur"]["colour"] = [round(c, 3) for c in tint]
        if not paint.set_wheel_blur_colour(stream, full, tint):
            log("wheel blur: the host has no g_vec4WheelBlurColour; the disc keeps the material's white")
    return report


def entry_sizes(vehc: Entry) -> dict[str, int]:
    """Serialized size of a VEHC's children (MDXN, NONR), edits included."""
    return {c.tag: entry_size(c) for c in vehc.children}
