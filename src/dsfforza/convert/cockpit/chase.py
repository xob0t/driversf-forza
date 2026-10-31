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
    (fm.COCKPIT_EXTERIOR_SECTIONS), the upgrades (SKIP_SECTIONS), the lamps, the glass
    sections (glass* and FM4's lexan*, damage.host.glass_name) and every glass
    subsection: the exterior entry has the main carbin's. Sections the converter makes
    for the cockpit view (dsf_*: virtual displays and their panels, moved wheel displays)
    stay out too: they stand in for Forza's displays from the driver's seat, and a panel
    on the ARX-02a's dash stood up through its bodywork from outside."""
    from dsfforza.convert.damage.host import glass_name

    out = []
    for name, s in static.items():
        if (
            fm.COCKPIT_EXTERIOR_SECTIONS.match(name)
            or name in SKIP_SECTIONS
            or LIGHT_SECTIONS.search(name)
            or glass_name(name).startswith("glass")
            or name.lower().startswith("dsf_")
        ):
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
    out = []
    for rear in (False, True):
        kept = visible_triangles(
            cabin + occluders, chase_eyes(chase.place, rear), resolution, near_keep=0.0, tiny_slack=0.005
        )
        culled = cull_sections(cabin, {k: v for k, v in kept.items() if k[0] < len(cabin)})
        out.append([s for s in culled if s.subsections])

    def count(sections):
        return sum(len(u.triangles()) for s in sections for u in s.subsections if u.lod == 0)

    lod1 = sum(
        len(u.triangles())
        for s in chase.body
        if s.name.lower() in LOD1_CABIN
        for u in s.subsections
        if u.lod == chase.lod
    )
    chase.report = {
        "cockpit_cabin_triangles": count(cabin),
        "seen_from_the_ring": count(out[0]),
        "seen_from_its_rear_half": count(out[1]),
        "lod1_cabin_triangles": lod1,
    }
    return out[0], out[1]


# --- the interior atlas, planned for the head view alone ---------------------------------
#
# The cockpit view keeps its atlas as it was: plan() lays out the regions the head-culled
# cockpit needs, at the scale they get without the chase cabin (on 17 of 555 cars the
# chase cabin's texels would have shrunk it: the Panoz LMP 03's outside parts from 1.0 to
# 0.25, the Agera's cabin from 0.92 to 0.82; roster survey, research/cockpit.md). A chase
# triangle whose texels a planned region holds maps into it; the others draw flat, in the
# converted mean of their own texels (one cell per subsection).


def region_of(t, family: str, uvs):
    """The planned region of `family` whose source rectangle holds every UV of a triangle
    (atlas UVs, clamped as Texturing.triangle_uvs clamps them), else None."""
    w, h = t.src_sizes[family]
    pts = [(min(1.0, max(0.0, u)) * w, min(1.0, max(0.0, v)) * h) for u, v in uvs]
    for r in t.regions.get(family, ()):
        if all(r.src[0] <= x <= r.src[2] and r.src[1] <= y <= r.src[3] for x, y in pts):
            return r
    return None


def _atlas_textured(t, section, sub):
    """Texturing's SubMaterial when the subsection samples a planned atlas family as a
    picture (not a swatch, tiling, flat, digital display), else None."""
    from dsfforza.convert.cockpit.geometry import DIGITAL_TECHNIQUES, FORCE_FLAT

    if sub.material.lower() in DIGITAL_TECHNIQUES or FORCE_FLAT.match(sub.material) or getattr(section, "flat", False):
        return None
    m = t.submaterial(section, sub)
    return m if m.kind == "textured" and t._uses_atlas(m) else None


def chase_flats(t, sections) -> dict[int, tuple]:
    """id(subsection) -> its flat cell key ("flat", r, g, b, a): the converted mean of the
    source texels at the corners and centre of each triangle no planned region holds,
    for every chase subsection that has such triangles. `t` is planned for the head view."""
    from dsfforza.convert import dsf_materials as dm

    out = {}
    for s in sections:
        for sub in s.subsections:
            m = _atlas_textured(t, s, sub)
            if m is None:
                continue
            w, h, rgba = t.atlases.get(m.family)
            pool = s.vertices_for(sub)
            acc, n = [0, 0, 0, 0], 0
            for tri in sub.triangles():
                uvs = dm.triangle_uv(m, [pool[i].uv0 for i in tri])
                if region_of(t, m.family, uvs) is not None:
                    continue
                centre = (sum(u for u, _ in uvs) / 3, sum(v for _, v in uvs) / 3)
                for u, v in [*uvs, centre]:
                    x = min(w - 1, max(0, int(min(1.0, max(0.0, u)) * w)))
                    y = min(h - 1, max(0, int(min(1.0, max(0.0, v)) * h)))
                    o = (y * w + x) * 4
                    for c in range(4):
                        acc[c] += rgba[o + c]
                    n += 1
            if n:
                out[id(sub)] = ("flat", *t._convert_texel(m.family, *(round(a / n) for a in acc)))
    return out


class ChaseBinding:
    """add_geometry's binding for the chase cabin: CockpitBinding (the interior's materials
    and atlas), with textured triangles mapped into the head view's regions or onto their
    subsection's flat cell (chase_flats)."""

    def __init__(self, inner, flats: dict):
        self.inner, self.texturing, self.flats = inner, inner.texturing, flats

    def material(self, section, sub, shared):
        if _atlas_textured(self.texturing, section, sub) is not None:
            return self.inner.materials.textured
        return self.inner.material(section, sub, shared)

    def triangle_uvs(self, section, sub, triangle_index, raw_uvs):
        from dsfforza.convert import dsf_materials as dm

        t = self.texturing
        m = _atlas_textured(t, section, sub)
        if m is None:
            return self.inner.triangle_uvs(section, sub, triangle_index, raw_uvs)
        uvs = dm.triangle_uv(m, raw_uvs)
        r = region_of(t, m.family, uvs)
        if r is None:
            key = self.flats[id(sub)]
            return [t.solid_uv(key)] * len(uvs), key
        size, scale = t.src_sizes[m.family], t._scale_of(r)
        mapped = [r.map(min(1.0, max(0.0, u)), min(1.0, max(0.0, v)), size, scale, t.size) for u, v in uvs]
        return mapped, (m.family, t.regions[m.family].index(r))

    @property
    def paint_uv(self):
        return self.inner.paint_uv
