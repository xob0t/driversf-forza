"""The Forza car: its folder, carbins and brake rotors, and its dimensions (CarDims)."""

from __future__ import annotations

import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from dsfforza.convert.forza2dsf.materials import vanilla_sections
from dsfforza.convert.forza2dsf.wheels import ROTOR_CARBINS
from dsfforza.formats import carbin

# --- the Forza car --------------------------------------------------------------


def find_file(folder: Path, relative: str) -> Path | None:
    """Case-insensitive lookup of a relative path in an extracted car folder
    (FH1 has Physics/MAXData.xml and NIS_370Z_10.carbin, FM4 physics/maxdata.xml
    and nis_370z_10.carbin)."""
    here = Path(folder)
    for part in relative.replace("\\", "/").split("/"):
        if not here.is_dir():
            return None
        here = next((p for p in here.iterdir() if p.name.lower() == part.lower()), None)
        if here is None:
            return None
    return here


@dataclass
class ForzaCar:
    name: str
    folder: Path
    sections: list
    body: list  # vanilla body sections (upgrade variants dropped), wheel excluded
    wheel: list
    # {"front"|"rear": sections of the left rotor carbin} (ROTOR_CARBINS, brake_rotor_section)
    rotors: dict = field(default_factory=dict)

    @property
    def game(self) -> str:
        return carbin.game_of(self.sections)


# The wheel archive's carbin (media/wheels/<car>), cached by port_car.extract_wheel
# under this name: one section `wheel`, the same model as the main carbin's.
WHEEL_CARBIN = "wheel.carbin"


def _has_tyre(wheel_sections) -> bool:
    return any(
        sub.lod > 0 and sub.material.lower().startswith(("tire", "tyre"))
        for s in wheel_sections
        for sub in s.subsections
    )


def load_car(folder: Path, name: str) -> ForzaCar:
    """The car's main carbin. The wheel is its `wheel` section; without one, the
    vanilla variant of a `wheela`/`wheelb` pair (the Minis), else the `wheel` section
    of the wheel archive's carbin (Ferrari 360 and Dino, Gallardo 05, Esperante,
    Corvette Grand Sport: their main carbin has no wheel). The wheel archive's also
    stands in for a main-carbin wheel with no tyre at any LOD: MER_SLR_05's is one mesh on
    its `4_MER_SLR_05_damage` technique, AST_V12Zagato_12's `body` and `interior` (tyre and
    rim in one), which drew as flat grey discs; their wheel archives hold the real wheel
    (tire, rim, inner_rim, blur_rim; the same size), #407."""
    path = find_file(folder, f"{name}.carbin")
    if path is None:
        raise FileNotFoundError(f"{name}.carbin not in {folder}")
    sections = carbin.parse(path.read_bytes())
    vanilla = vanilla_sections(sections)
    wheel = [s for s in sections if s.name == "wheel"]
    if not wheel:
        wheel = [s for s in vanilla if re.fullmatch(r"wheel[a-z]", s.name)]
    if not _has_tyre(wheel):
        archive = find_file(folder, WHEEL_CARBIN)
        if archive is not None:
            own = [s for s in carbin.parse(archive.read_bytes()) if s.name == "wheel"]
            if own and (not wheel or _has_tyre(own)):
                wheel = own
    if not wheel:
        raise ValueError(f"{name}: no wheel section (main carbin or {WHEEL_CARBIN})")
    taken = {id(s) for s in wheel}
    return ForzaCar(
        name,
        Path(folder),
        sections,
        [s for s in vanilla if s.name != "wheel" and id(s) not in taken],
        wheel,
        load_rotors(folder),
    )


def load_rotors(folder: Path) -> dict:
    """{"front"|"rear": sections} of the car's cached rotor carbins (ROTOR_CARBINS); an axle
    without one, or whose carbin doesn't parse, is left out."""
    rotors = {}
    for axle, name in ROTOR_CARBINS.items():
        path = find_file(folder, name)
        if path is None:
            continue
        try:
            sections = carbin.parse(path.read_bytes())
        except (carbin.CarbinError, struct.error, IndexError):
            continue
        if any(sub.triangles() for s in sections for sub in s.subsections):
            rotors[axle] = sections
    return rotors


@dataclass
class CarDims:
    """Where the Forza car's wheels are, in Forza model space (metres)."""

    wheelbase: float
    # Forza model z of the wheelbase midpoint, where the arches centre: gamedb.slt's
    # Data_CarBody.BottomCenterWheelbasePosZ, else MINUS MAXData's BottomCenterWheelbasePos.z
    # plus the wheel model's own z offset (car_dims). MAXData's z is measured towards the
    # front (+z forward) while the model's front is at -z: the wheel arches of the Cuda,
    # Mustang Boss 429 and Veyron centre on -z (-0.24 / -0.20 / -0.12 against z = +0.203 /
    # +0.213 / +0.122); the gamedb's value is the model's own z (MAXData's negated on 558
    # of the 573 roster cars).
    mid_z: float
    track_outer: float  # front track, outer tyre wall to outer tyre wall
    radius: float  # tyre radius, measured on the wheel model
    tyre_width: float  # measured on the wheel model
    source: str  # where wheelbase/track/mid_z came from
    # How far Forza's ground lies below the model's y = 0: StockRideHeight, plus how far
    # below y = 0 the body's bottom is (a negative BottomCenterWheelbasePos.y). Model y = 0
    # is the bottom of the body at the wheelbase centre on 549 of the 573 cars (y 0; 370Z:
    # ride height 0.144, MAXData's collision box reaches y = -0.14, the body itself -0.055);
    # without the ride height the body sat 0.14 to 0.23 m too low on its wheels. 15 cars
    # have a negative y, where their floor is: VW_BEETLE_68's -0.158 (floor -0.143 to
    # -0.169) puts its ground 0.316 m down, where its arches centre, and gamedb.slt's own
    # ride height for it is 0.314 with y 0 (5 more of the 15 have y folded into the gamedb's
    # ride height the same way). The 9 with a positive y (+0.024 to +0.031: the Veyron 09,
    # Z4 11, Furai) have their floors below y = 0, not at y, and raised by it the Furai's
    # body would reach 8 mm into the ground; their y is left out (research/wheels.md, "Body
    # height and wheelbase", #406).
    ride_height: float = 0.0
    rear_track_outer: float | None = None  # MAXData RearTrackOuter (None: as the front)
    # The tyre's centre in the wheel model's frame. (0, 0, 0) on nearly every car; the
    # wheel is turned about it, not about the model's origin (tyre_centre).
    tyre_centre: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @property
    def track(self) -> float:
        """Hub to hub, at the model's tyre width (convert scales the wheel to Data_Car's)."""
        return self.track_outer - self.tyre_width


def _tyre_points(wheel_sections) -> list:
    subs = [(s, sub) for s in wheel_sections for sub in s.subsections if sub.lod > 0]
    tyres = [(s, sub) for s, sub in subs if sub.material in ("tire", "tyre")] or subs
    lod = min(sub.lod for _, sub in tyres)
    return [s.vertices_for(sub)[i].position for s, sub in tyres if sub.lod == lod for t in sub.triangles() for i in t]


def tyre_size(wheel_sections) -> tuple[float, float]:
    """(radius, width) of the tyre in the wheel model's lowest non-zero LOD."""
    pts = _tyre_points(wheel_sections)
    return ((max(p[1] for p in pts) - min(p[1] for p in pts)) / 2, max(p[0] for p in pts) - min(p[0] for p in pts))


def tyre_centre(wheel_sections) -> tuple[float, float, float]:
    """The centre of the tyre's bounding box in the wheel model's frame: the axle the
    wheel turns about. Within a millimetre of the origin on nearly every Forza wheel;
    TOY_6_Supra_05's sits 49 mm out and 80 mm down (its section offset), so drawn about
    the origin it hung 80 mm into the road and wobbled as it turned. Coordinates under
    1 mm count as 0."""
    pts = _tyre_points(wheel_sections)
    centre = [(max(p[k] for p in pts) + min(p[k] for p in pts)) / 2 for k in range(3)]
    return tuple(0.0 if abs(c) < 0.001 else c for c in centre)


def gamedb_body(gamedb: Path, name: str) -> tuple[float, float, float, float, float, float] | None:
    """(wheelbase, front track outer, wheelbase midpoint z, body bottom y, ride height, rear
    track outer) from a game's gamedb.slt: Data_CarBody row Data_Car.Id * 1000
    (ModelWheelbase, ModelFrontTrackOuter, BottomCenterWheelbasePosZ and y, the mean of
    ModelFront/RearStockRideHeight, ModelRearTrackOuter). Every roster car has its row in its
    own game's gamedb."""
    import sqlite3

    db = sqlite3.connect(f"file:{Path(gamedb).as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT b.ModelWheelbase, b.ModelFrontTrackOuter, b.BottomCenterWheelbasePosZ, "
            "b.BottomCenterWheelbasePosy, (b.ModelFrontStockRideHeight + b.ModelRearStockRideHeight) / 2, "
            "b.ModelRearTrackOuter "
            "FROM Data_Car c JOIN Data_CarBody b ON b.Id = c.Id * 1000 "
            "WHERE lower(c.MediaName) = lower(?)",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return tuple(row) if row and row[0] else None


GAMEDB = "gamedb.slt"  # port_car.extract_gamedb's copy, beside the game's car folders


def cached_gamedb(folder: Path) -> Path | None:
    """The car's game's gamedb.slt in the cache (<cache>/<game>/gamedb.slt next to
    <cache>/<game>/<car>), or None."""
    path = Path(folder).parent / GAMEDB
    return path if path.exists() else None


_FIT_CACHE: dict = {}


def arch_fit(car: ForzaCar, gamedb, mid_z: float, wheelbase: float, track: tuple, ride_height: float, radius, width):
    """How many of the car's wheel readings fall within vanilla's range (wheel_fit.in_band:
    each wheel's top gap and lateral offset against its arch, wheel_fit.measure_wheel) with
    the wheels at `track` (front, rear track outer) and `ride_height`, on the Forza body
    (LOD 1, the exterior's) at Forza's static pose, which settle_wheels keeps at rest in game.
    The tyres are the axles' Data_Car sizes as convert draws them (axle_fits)."""
    import numpy as np

    from dsfforza.convert import wheel_fit
    from dsfforza.convert.forza2dsf.wheels import axle_fits, axle_tyres

    key = (str(car.folder), car.name, round(mid_z, 5), round(wheelbase, 5), tuple(round(t, 5) for t in track))
    key += (round(ride_height, 5),)
    if key in _FIT_CACHE:
        return _FIT_CACHE[key]
    lod = min((sub.lod for s in car.body for sub in s.subsections if sub.lod > 0), default=1)
    tris = np.array(
        [
            [s.vertices_for(sub)[i].position for i in t]
            for s in car.body
            for sub in s.subsections
            if sub.lod == lod
            for t in sub.triangles()
        ],
        dtype=np.float64,
    ).reshape(-1, 3, 3)
    fits = axle_fits(radius, width, axle_tyres(car.name, [gamedb] if gamedb else []))
    count = 0
    for k, axle in enumerate(("front", "rear")):
        r, w = radius * fits[axle][1], width * fits[axle][0]
        z = mid_z + (-1 if axle == "front" else 1) * wheelbase / 2  # Forza's front is at -z
        for side in (1.0, -1.0):
            centre = np.array([side * (track[k] - w) / 2, -ride_height + r, z])
            m = wheel_fit.measure_wheel(tris, centre, r, w, side)
            if m is None:
                continue
            count += wheel_fit.in_band("top_gap_mm", axle, None if m[3] is None else m[3] * 1000)
            count += wheel_fit.in_band("lateral_mm", axle, None if m[4] is None else m[4] * 1000)
    _FIT_CACHE[key] = count
    return count


def car_dims(car: ForzaCar, gamedb: Path | None = None) -> CarDims:
    """Wheel placement data. `gamedb`: the car's game's gamedb.slt (default: the cached
    one, cached_gamedb).

    - Wheelbase and its midpoint: gamedb.slt Data_CarBody (ModelWheelbase,
      BottomCenterWheelbasePosZ), else Physics/MAXData.xml. The two agree on 536 of the 573
      roster cars; where they don't, the body's arches centre on the gamedb's hubs (35 cars:
      AST_V8Vantage_77's front arches 78 mm ahead of MAXData's hubs, 11 of the gamedb's;
      PAG_Huayra_12, SAL_S7_04, BUS_TOUR_12 and CIT_C1_11, which carries PEU_107_11's
      MAXData.xml, 90 to 380 mm off MAXData's, within 15 of the gamedb's). The handling's
      wheelbase (VO3D p392, the hubs' on every vanilla car) is the gamedb's too
      (research/wheels.md, "Body height and wheelbase", #406).
    - Track and ride height: MAXData.xml (<Misc FrontTrackOuter RearTrackOuter
      Front/RearStockRideHeight>, the ride height the mean of both, plus a negative
      BottomCenterWheelbasePos.y: CarDims.ride_height), or the gamedb's (ModelFront/
      RearTrackOuter, the ModelStockRideHeights) when its wheels fit the car's arches better:
      more of their readings (each wheel's top gap and lateral offset, wheel_fit) within the
      vanilla cars' range (arch_fit; a tie keeps MAXData.xml). Neither source fits every car:
      SAL_S7_04's MAXData.xml is another car's (track 1.613 against the gamedb's 1.935, its
      tyres 0.16 m inboard of the fenders and 0.13 m under the arch tops), while the gamedb's
      track would put AUD_A4Avant_04's tyres 30 mm outside its fenders and its ride height
      would lift CHR_JeepGCSRT8_12's body 110 mm higher over its wheels (research/wheels.md,
      "Track and ride height per car", #417).
    Tyre radius, width and centre always come from the car's own wheel model.

    A wheel model whose tyre sits off its origin along the car (z): Forza draws the model
    unturned at each hub, so its tyres stand that far behind the physics' hubs and the
    arches are cut round them (the five Commodore VE race cars' tyres 0.300 m behind their
    origin). The gamedb's midpoint is already there; MAXData's is the physics' hub, so
    without a gamedb row the tyre centre's z is added. The converter turns the wheel about
    the tyre's centre (tyre_centre), so it takes the hubs to the tyres. Across (x) and up (y)
    the tyre stays on the hub: TOY_6_Supra_05's, MER_ML63AMG_09's and VW_BEETLE_68's arches
    centre on the hub, not on their models' offsets."""
    radius, width = tyre_size(car.wheel)
    centre = tyre_centre(car.wheel)
    gamedb = gamedb if gamedb is not None else cached_gamedb(car.folder)
    body = gamedb_body(gamedb, car.name) if gamedb is not None and Path(gamedb).exists() else None
    maxdata = find_file(car.folder, "Physics/MAXData.xml")
    if maxdata is not None:
        misc = ET.parse(maxdata).getroot().find("Misc")
        bottom = misc.find("BottomCenterWheelbasePos")
        ride = [float(misc.get(k)) for k in ("FrontStockRideHeight", "RearStockRideHeight") if misc.get(k)]
        ride_height = (sum(ride) / len(ride) if ride else 0.0) - min(0.0, float(bottom.get("y") or 0.0))
        track = (float(misc.get("FrontTrackOuter")), float(misc.get("RearTrackOuter") or misc.get("FrontTrackOuter")))
        if body is not None:
            wheelbase, mid_z, source = body[0], body[2], f"MAXData.xml, {Path(gamedb).name} wheelbase"
            own = ((body[1], body[5] or body[1]), (body[4] or 0.0) - min(0.0, body[3] or 0.0))
            if max(abs(own[0][0] - track[0]), abs(own[0][1] - track[1]), abs(own[1] - ride_height)) > 0.0005:
                here = arch_fit(car, gamedb, mid_z, wheelbase, track, ride_height, radius, width)
                there = arch_fit(car, gamedb, mid_z, wheelbase, own[0], own[1], radius, width)
                if there > here:  # the gamedb's wheels fit the arches better: its track and ride height
                    track, ride_height = own
                    source = (
                        f"{Path(gamedb).name} Data_CarBody (track and ride height: {there} of the wheels' "
                        f"readings in vanilla's range, MAXData.xml's {here})"
                    )
        else:
            wheelbase, mid_z, source = float(misc.get("Wheelbase")), -float(bottom.get("z")) + centre[2], "MAXData.xml"
        return CarDims(wheelbase, mid_z, track[0], radius, width, source, ride_height, track[1], centre)
    if body is not None:
        wheelbase, track, mid_z, y, ride, rear = body
        source = f"{Path(gamedb).name} Data_CarBody"
        return CarDims(
            wheelbase, mid_z, track, radius, width, source, (ride or 0.0) - min(0.0, y or 0.0), rear, tyre_centre=centre
        )
    raise ValueError(f"{car.name}: no Physics/MAXData.xml and no gamedb Data_CarBody row")
