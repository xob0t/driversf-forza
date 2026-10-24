"""The Bonnet camera of a converted car, placed on its Forza windscreen.

DSF's camera modes (LuaScripts cameraselection.lua: Normal, DriverEye, Bonnet,
Bumper) take their mounts from VO3D (research/cockpit.md, "Cameras"). Measured in
game (clonetest.lua logs the camera in the car's frame, run vm-20261024-053821):

- Bonnet: (p535, p536 + 0.30, p537) above the ground, +z forward (147: p536 0.91,
  p537 0.87 -> (0, 1.21, 0.87)). 140 stock cars set p536/p537 per car: the camera
  sits just in front of the windscreen's centre, at the role-63 marker (on the
  windscreen, 57% of its height up the centre line) + (0, 0.058, 0.085) (fleet
  medians, quartiles 0.014..0.094 and 0.028..0.154; out/cameras/bonnet_fleet.py).
- Bumper: (p327, p328 + 0.30, z from the host's collision box + p329); no stock car
  sets them, the player's car is hidden in that view, so they stay the host's.
- The ground those heights count from is the host's: model y = highest hub y - p360
  of the host as shipped (147, 266, 205, 278 within 3 mm, 163 within 8 mm).

A converted car kept the host's p535..537, so on a car lower than its host the
camera sat inside the cabin behind the glass (the Agera on host 163: 6 cm under
its roof top). bonnet_params puts it at the same place relative to the Forza
windscreen (`glassF`) as stock cars have it relative to theirs.
"""

from __future__ import annotations

import math
import struct

BONNET = (535, 536, 537)
BONNET_LIFT = 0.30  # in game: p536 + 0.30 above the ground
BONNET_OFFSET = (0.0, 0.058, 0.085)  # camera - windscreen centre, stock fleet median (DSF model space)
WINDSCREEN_HEIGHT = 0.57  # the role-63 marker's place up the windscreen (stock median)
CENTRE_BAND = 0.15  # m either side of the centre line
MIN_CLEARANCE = 0.09  # m over the car below it (stock: 10th percentile 0.087, median 0.12)
WINDSCREEN_SECTION = "glassF"


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def ray_hit(origin, direction, tri) -> float | None:
    """Distance along a unit ray to a triangle (Moller-Trumbore), or None."""
    p0, p1, p2 = tri
    e1, e2 = _sub(p1, p0), _sub(p2, p0)
    h = _cross(direction, e2)
    a = _dot(e1, h)
    if abs(a) < 1e-12:
        return None
    f = 1 / a
    s = _sub(origin, p0)
    u = f * _dot(s, h)
    if u < 0 or u > 1:
        return None
    q = _cross(s, e1)
    v = f * _dot(direction, q)
    if v < 0 or u + v > 1:
        return None
    t = f * _dot(e2, q)
    return t if t > 0 else None


def first_hit(tris, origin, direction, reach: float = math.inf) -> float | None:
    best = None
    for tri in tris:
        t = ray_hit(origin, direction, tri)
        if t is not None and t <= reach and (best is None or t < best):
            best = t
    return best


def windscreen_centre(tris) -> tuple[float, float, float] | None:
    """The point on a windscreen (triangles in DSF model space, +z forward) on the
    centre line, WINDSCREEN_HEIGHT of the way up: where stock cars put their
    role-63 marker."""
    if not tris:
        return None
    # The glass's extent on the centre line: rays from ahead every centimetre.
    band = [t for t in tris if min(p[0] for p in t) <= CENTRE_BAND and max(p[0] for p in t) >= -CENTRE_BAND]
    if not band:
        return None
    lo, hi = min(p[1] for t in band for p in t), max(p[1] for t in band for p in t)
    front = max(p[2] for t in band for p in t) + 1.0
    hits = [
        y
        for y in (lo + 0.01 * k for k in range(int((hi - lo) / 0.01) + 1))
        if first_hit(band, (0.0, y, front), (0.0, 0.0, -1.0)) is not None
    ]
    if not hits:
        return None
    y = min(hits) + WINDSCREEN_HEIGHT * (max(hits) - min(hits))
    t = first_hit(band, (0.0, y, front), (0.0, 0.0, -1.0))
    return None if t is None else (0.0, y, front - t)


def section_triangles(sections, place, lod: int | None = None) -> list[tuple]:
    """Triangles of Forza sections in DSF model space, each section at `lod` or,
    without it (or None), at its finest LOD."""
    tris = []
    for section in sections:
        lods = {sub.lod for sub in section.subsections}
        if not lods:
            continue
        use = lod if lod in lods else min(lods)
        for sub in section.subsections:
            if sub.lod != use:
                continue
            pool = section.vertices_for(sub)
            for tri in sub.triangles():
                tris.append(tuple(place.point(pool[i].position) for i in tri))
    return tris


def forza_windscreen(body_sections, place) -> list[tuple]:
    """The Forza windscreen (`glassF`) at its finest LOD, in DSF model space."""
    return section_triangles([s for s in body_sections if s.name == WINDSCREEN_SECTION], place)


def bonnet_position(windscreen, body_tris) -> tuple[tuple[float, float, float], dict]:
    """The camera at the windscreen centre + BONNET_OFFSET, raised to at least
    MIN_CLEARANCE over the body below it (a long, flat windscreen can put the
    offset point just over the bonnet). Returns it and what the rays found:
    up/ahead within 2 m / 1.5 m would mean inside the cabin or a blocked view."""
    cam = tuple(w + o for w, o in zip(windscreen, BONNET_OFFSET))
    raised = 0.0
    below = first_hit(body_tris, cam, (0.0, -1.0, 0.0), reach=3.0)
    for _ in range(8):  # glass has two skins: rising above one can meet the next
        if below is None or below >= MIN_CLEARANCE - 1e-6:
            break
        raised += MIN_CLEARANCE - below
        cam = (cam[0], cam[1] + MIN_CLEARANCE - below, cam[2])
        below = first_hit(body_tris, cam, (0.0, -1.0, 0.0), reach=3.0)
    above = first_hit(body_tris, cam, (0.0, 1.0, 0.0), reach=2.0)
    ahead = first_hit(body_tris, cam, (0.0, 0.0, 1.0), reach=1.5)

    def r(t):
        return None if t is None else round(t, 3)

    return cam, {"over": r(below), "raised": round(raised, 3), "above": r(above), "ahead": r(ahead)}


def host_ground(upvh_data: bytes, vo3d_data: bytes) -> float:
    """Model y of the ground the engine counts camera heights from (the host as shipped)."""
    from dsfforza.build import handling
    from dsfforza.formats import upvh

    hubs = [struct.unpack_from("<3f", w)[1] for w in upvh.parse(upvh_data).table("wheels")]
    return max(hubs) - handling.VO3D.parse(vo3d_data).get(360)


def bonnet_camera(vo3d_data: bytes, ground: float) -> tuple[float, float, float]:
    """Where the Bonnet camera is, in DSF model space."""
    from dsfforza.build import handling

    v = handling.VO3D.parse(vo3d_data)
    return (v.get(BONNET[0]), v.get(BONNET[1]) + BONNET_LIFT + ground, v.get(BONNET[2]))


def bonnet_params(cam, ground: float) -> dict[int, float]:
    """VO3D p535..537 that put the Bonnet camera at `cam` (DSF model space)."""
    return {BONNET[0]: cam[0], BONNET[1]: cam[1] - BONNET_LIFT - ground, BONNET[2]: cam[2]}


def patch_vo3d(vo3d_data: bytes, params: dict[int, float]) -> bytes:
    from dsfforza.build import handling

    v = handling.VO3D.parse(vo3d_data)
    v.params.update(params)
    return v.build()
