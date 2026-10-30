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


def load_car(folder: Path, name: str) -> ForzaCar:
    """The car's main carbin. The wheel is its `wheel` section; without one, the
    vanilla variant of a `wheela`/`wheelb` pair (the Minis), else the `wheel` section
    of the wheel archive's carbin (Ferrari 360 and Dino, Gallardo 05, Esperante,
    Corvette Grand Sport: their main carbin has no wheel)."""
    path = find_file(folder, f"{name}.carbin")
    if path is None:
        raise FileNotFoundError(f"{name}.carbin not in {folder}")
    sections = carbin.parse(path.read_bytes())
    vanilla = vanilla_sections(sections)
    wheel = [s for s in sections if s.name == "wheel"]
    if not wheel:
        wheel = [s for s in vanilla if re.fullmatch(r"wheel[a-z]", s.name)]
    if not wheel:
        archive = find_file(folder, WHEEL_CARBIN)
        if archive is not None:
            wheel = [s for s in carbin.parse(archive.read_bytes()) if s.name == "wheel"]
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
    # Forza model z of the wheelbase midpoint: MINUS BottomCenterWheelbasePos.z.
    # That value is measured towards the front (+z forward) while the model's
    # front is at -z: the wheel arches of the Cuda, Mustang Boss 429 and Veyron
    # centre on -z (-0.24 / -0.20 / -0.12 against z = +0.203 / +0.213 / +0.122).
    # Taking +z put those cars' wheels 2 x z (0.25 to 0.43 m) off their arches.
    # Plus the wheel model's own z offset (tyre_centre[2], car_dims): the arches centre on
    # where Forza draws the tyre, not on the physics' hub.
    mid_z: float
    track_outer: float  # front track, outer tyre wall to outer tyre wall
    radius: float  # tyre radius, measured on the wheel model
    tyre_width: float  # measured on the wheel model
    source: str  # where wheelbase/track/mid_z came from
    # Forza model y = 0 is the bottom of the body at the wheelbase centre
    # (BottomCenterWheelbasePos), not the ground: the ground is StockRideHeight
    # below it (370Z: 0.144; MAXData's collision box reaches y = -0.14, the
    # body itself -0.055). Without it the body sat 0.14 to 0.23 m too low on
    # its wheels, tyres through the arches.
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


def gamedb_body(gamedb: Path, name: str) -> tuple[float, float, float, float] | None:
    """(wheelbase, front track outer, wheelbase midpoint z, ride height) from a game's
    gamedb.slt: Data_CarBody row Data_Car.Id * 1000. Holds 503 of FM4's cars."""
    import sqlite3

    db = sqlite3.connect(f"file:{Path(gamedb).as_posix()}?mode=ro", uri=True)
    try:
        row = db.execute(
            "SELECT b.ModelWheelbase, b.ModelFrontTrackOuter, b.BottomCenterWheelbasePosZ, "
            "(b.ModelFrontStockRideHeight + b.ModelRearStockRideHeight) / 2 "
            "FROM Data_Car c JOIN Data_CarBody b ON b.Id = c.Id * 1000 "
            "WHERE lower(c.MediaName) = lower(?)",
            (name,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    return tuple(row) if row else None


# Wheelbase midpoints (Forza model z, CarDims.mid_z) of cars whose MAXData.xml doesn't fit
# their body. CIT_C1_11 carries PEU_107_11's MAXData.xml value for value
# (BottomCenterWheelbasePos z -0.151, wheelbase 2.340), but its body sits 0.17 m further
# forward in the model than the 107's (body length midpoint -0.124 against +0.048), so its
# wheels stood 0.17 m behind its arches (the arches centre on -0.024, the 107's
# 5 mm off its own midpoint the same way). The only car of the roster's 573 whose arches
# both lie more than 60 mm off MAXData's hubs the same way (research/wheels.md, "Wheels off
# their arches").
MID_Z_FIXES = {"cit_c1_11": 0.151 - 0.170}


def car_dims(car: ForzaCar, gamedb: Path | None = None) -> CarDims:
    """Wheel placement data, from the first source that has it (ride height:
    mean of the front and rear StockRideHeight):

    1. Physics/MAXData.xml in the car's zip (<Misc Wheelbase FrontTrackOuter>,
       <BottomCenterWheelbasePos z>): every FH1 and FM4 car on the discs but
       FH1's NIS_Leaf_11. FM4 cars carry the same file as FH1 (lower-case path).
    2. gamedb.slt Data_CarBody (ModelWheelbase, ModelFrontTrackOuter,
       BottomCenterWheelbasePosZ), when a gamedb is given.
    Tyre radius, width and centre always come from the car's own wheel model.

    A wheel model whose tyre sits off its origin along the car (z) moves the wheelbase
    midpoint by that much. Forza draws the model unturned at each hub, so its tyres stand
    that far behind the physics' hubs on both sides, and the body's arches are cut round
    them: the five Commodore VE race cars' tyres are 0.300 m behind their origin, and their
    arches 0.295 to 0.310 m behind MAXData's hubs. The converter turns the
    wheel about the tyre's centre (tyre_centre), so it takes the hubs to the tyres instead.
    Across (x) and up (y) the tyre stays on the hub: TOY_6_Supra_05's, MER_ML63AMG_09's
    and VW_BEETLE_68's arches centre on the hub, not on their models' offsets. A car of
    MID_Z_FIXES takes its midpoint from there instead of MAXData's."""
    radius, width = tyre_size(car.wheel)
    centre = tyre_centre(car.wheel)
    maxdata = find_file(car.folder, "Physics/MAXData.xml")
    if maxdata is not None:
        misc = ET.parse(maxdata).getroot().find("Misc")
        ride = [float(misc.get(k)) for k in ("FrontStockRideHeight", "RearStockRideHeight") if misc.get(k)]
        mid_z = MID_Z_FIXES.get(car.name.lower(), -float(misc.find("BottomCenterWheelbasePos").get("z")))
        return CarDims(
            float(misc.get("Wheelbase")),
            mid_z + centre[2],
            float(misc.get("FrontTrackOuter")),
            radius,
            width,
            "MAXData.xml",
            sum(ride) / len(ride) if ride else 0.0,
            float(misc.get("RearTrackOuter")) if misc.get("RearTrackOuter") else None,
            centre,
        )
    if gamedb is not None and Path(gamedb).exists():
        row = gamedb_body(gamedb, car.name)
        if row:
            source = f"{Path(gamedb).name} Data_CarBody"
            return CarDims(
                row[0], -row[2] + centre[2], row[1], radius, width, source, row[3] or 0.0, tyre_centre=centre
            )
    raise ValueError(f"{car.name}: no Physics/MAXData.xml and no gamedb Data_CarBody row")
