"""cockpit.fit: the whole cockpit measurement (CockpitFit) of one car."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dsfforza.convert import forza_materials as fm
from dsfforza.convert.cockpit.displays import dash_needles, digital_displays, flatten_face, virtual_gauges
from dsfforza.convert.cockpit.geometry import (
    DIGITAL_TECHNIQUES,
    GAUGE_IDS,
    LIGHT_SECTIONS,
    MAIN_CABIN,
    SKIP_SECTIONS,
    SPEED_DIAL_PARAM,
    SPEED_FLOOR_PARAM,
    STUB_EXTENT,
    STUB_TRIANGLES,
    CockpitFit,
    _mean,
    calibrate_gauge,
    cockpit_parts,
    eye_offset,
    eye_point,
    fit_gauge,
    fit_wheel,
    gauge_ticks,
    promote_lod,
    section_points,
    with_display_faces,
)
from dsfforza.formats import carbin


def is_stub(sections: list[carbin.Section]) -> bool:
    """FH1 ships placeholder cockpit carbins for some cars (Dodge Durango,
    Chevrolet Express): every section is a 17-vertex box about 0.2 m wide."""
    pts = [p for s in sections for p in section_points(s)]
    interior = sum(len(section_points(s)) // 3 for s in sections if s.name.lower() == "interior")
    # FH1 Taurus SHO: 17 sections of 8 triangles each, spread over the cabin.
    if len(pts) < 3 * STUB_TRIANGLES or interior < 100:
        return True
    return max(max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)) < STUB_EXTENT


def fit(
    sections: list[carbin.Section],
    name: str,
    gamedbs: list[Path],
    folder: Path | None = None,
    main_sections: list[carbin.Section] | None = None,
    virtual: bool = True,
) -> CockpitFit:
    """Measure the cockpit; `folder` (the extracted car) supplies the
    CarAttribs.xml tick tables for the gauge calibration. `main_sections`
    (the main carbin) stands in when the cockpit carbin is a stub (its
    interior, seats and steering wheel at their finest LOD, main atlases) or
    lacks a steering wheel (the main carbin's, drawn flat). With `virtual`, a
    speedometer or tachometer Forza shows as a digital readout gets a virtual
    dial on a blank display (virtual_gauges; the gamedbs give its scale)."""
    source, wheel_source, lod0 = "cockpit", "cockpit", True
    dropped: set = set()
    main = {s.name.lower(): s for s in (main_sections or [])}
    if is_stub(sections):
        cabin = [promote_lod(main[n.lower()]) for n in MAIN_CABIN if n.lower() in main]
        cabin = [c for c in cabin if c is not None]
        if not any(c.name.lower() == "interior" for c in cabin):
            raise ValueError(f"{name}: cockpit carbin is a stub and the main carbin has no interior section")
        # The main carbin's driver seat carries a baked low-poly driver (DSF seats
        # its own), whose head would sit around the eye: leave that seat out.
        wheel_at = next((c for c in cabin if c.name.lower() == "steering_wheel"), None)
        if wheel_at is not None:
            wx = _mean(section_points(wheel_at))[0]
            dropped = {
                c.name for c in cabin if c.name.lower().startswith("seat") and _mean(section_points(c))[0] * wx > 0
            }
            cabin = [c for c in cabin if c.name not in dropped]
        # forza2dsf passes [] when the car has no cockpit carbin at all.
        source = ("stub" if sections else "no") + " cockpit carbin: main carbin cabin"
        sections = cabin
        lod0 = False
    else:
        # Upgrade parts, as on the outside (forza2dsf.vanilla_sections): the cockpit
        # carbin holds every bonnet the car can wear (Mustang Boss 429: hooda,
        # hoodCIrace, hoodPSrace), all drawn at once over the vanilla one. The
        # Boss's body-colour race scoop hid hooda's black one from the cockpit.
        from dsfforza.convert import forza2dsf

        sections = forza2dsf.vanilla_sections(sections)
    static, needles = cockpit_parts(sections)
    if "steering_wheel" not in static:
        borrowed = promote_lod(main["steering_wheel"]) if "steering_wheel" in main else None
        if borrowed is None:
            raise ValueError(f"{name}: no steering_wheel section in the cockpit or main carbin")
        borrowed.flat = True  # main-carbin UVs don't fit the cockpit's _LOD0 atlases
        static["steering_wheel"] = borrowed
        wheel_source = "main carbin"
        if source == "cockpit":
            source = "cockpit; steering wheel from the main carbin"
    else:
        wheel_source = "cockpit" if lod0 else "main carbin"
    # Forza's wheel keeps its size in every view: dsfforza.asi [hands] moves the driver's
    # hands onto its rim (grip_params), the cockpit arms and the chase views' driver alike.
    wheel = fit_wheel(static["steering_wheel"])
    gauges = {}
    for family, poses in needles.items():
        g = fit_gauge(family, poses)
        if g is not None:
            gauges[family] = g
    offset, eye_source = eye_offset(name, gamedbs)
    mirrors = {}
    for s in static.values():
        for tech, side in (("mirrorLeft", "left"), ("mirrorRight", "right")):
            pts = section_points(s, {tech})
            if pts:
                mirrors[side] = _mean(pts)
    ticks = gauge_ticks(folder)
    calibration = {f: calibrate_gauge(g, ticks.get(f)) for f, g in gauges.items() if f in GAUGE_IDS}
    faces = 0
    if lod0:  # the face maps into interior_LOD0; main-carbin cabins have no digital_gauge
        for n, s in static.items():
            static[n], k = with_display_faces(s)
            faces += k
    virtual_report = {}
    digital_plan, digital_report = None, {}
    forza = None
    version = next((s.version for s in static.values()), 3)
    if virtual or lod0:
        from dsfforza.build import handling

        try:
            forza = handling.load_forza(name, [Path(db) for db in gamedbs if db and Path(db).exists()])
        except (SystemExit, LookupError, ValueError, sqlite3.Error):  # not in the gamedbs (as port_car's handling)
            forza = None
    if lod0 and folder is not None:
        # Forza's digital displays, live through DSF's digital dials (digital_dash); a
        # display only on the steering wheel on a virtual display (wheel_display_pod).
        sight = [s for n, s in static.items() if n not in SKIP_SECTIONS and not LIGHT_SECTIONS.search(n)]
        digital_plan, digital_report, extra = digital_displays(
            static, eye_point(wheel, offset), folder, forza, wheel, gauges, version, sight
        )
        static.update(extra)
        if digital_plan is not None:
            for g, cal in dash_needles(digital_plan, version):
                if g.family not in gauges:  # Forza's own needle first
                    gauges[g.family], calibration[g.family] = g, cal
                    needles[g.family] = {"min": g.rest}
    provided = set(digital_report.get("provides", ()))
    if virtual:
        drawn = [s for n, s in static.items() if n not in SKIP_SECTIONS and not LIGHT_SECTIONS.search(n)]
        try:
            atlas = fm.AtlasCache(folder, lod0=True).get(fm.INTERIOR) if folder is not None and lod0 else None
        except FileNotFoundError:
            atlas = None
        # Readouts the dash scene drives (digital_displays) need no virtual dial.
        readout = [tri for p in getattr(digital_plan, "patches", []) for tri in p.points]
        dials, virtual_report, extra = virtual_gauges(
            static,
            gauges,
            eye_point(wheel, offset),
            forza,
            version,
            drawn,
            wheel=wheel,
            atlas=atlas,
            readout=readout,
            provided=provided,
            # A placeholder cockpit carbin (the main carbin's cabin) has no
            # needle and no display; the car has a speedometer and a tachometer all the same.
            assume=() if lod0 else ("tach", "speed"),
        )
        if dials and "face" in virtual_report:
            faces -= flatten_face(static, virtual_report["face"])
        static.update(extra)
        for needle, face, g, cal in dials:
            needles[g.family] = {"min": needle}
            gauges[g.family] = g
            calibration[g.family] = cal
            static[face.name] = face
    digital = sum(
        len(sub.triangles())
        for s in static.values()
        for sub in s.subsections
        if sub.lod == 0 and sub.material.lower() in DIGITAL_TECHNIQUES
    )
    return CockpitFit(
        wheel,
        gauges,
        eye_point(wheel, offset),
        offset,
        eye_source,
        mirrors,
        static,
        needles,
        calibration,
        digital,
        source,
        wheel_source,
        lod0,
        dropped,
        display_faces=faces,
        virtual=virtual_report,
        digital_plan=digital_plan,
        digital_displays=digital_report,
    )


def gauge_params(cf: CockpitFit) -> dict[int, float]:
    """VO3D: the speedometer stops at its last mark (p605), from 0 (p606). Digital
    speed digits read the same clamped value: without a needle they go to 255 mph."""
    cal = cf.calibration.get("speed")
    if cal:
        return {SPEED_DIAL_PARAM: float(cal.dial_max), SPEED_FLOOR_PARAM: 0.0}
    if "speed" in cf.digital_displays.get("provides", ()):
        return {SPEED_DIAL_PARAM: 255.0, SPEED_FLOOR_PARAM: 0.0}
    return {}
