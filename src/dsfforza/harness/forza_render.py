"""The Forza reference render: a Forza car with Forza's own materials, placed as the
converter placed it, for `dsff render_compare` (research/render-compare.md).

The main carbin's vanilla body sections at one LOD (the LOD the converter drew the
entry from: `lods` in the build's per-car report), turned and moved into DSF car
space by the conversion's `Placement` (forza2dsf), and the Forza wheel model on the
hubs where the built car's wheels stand. Each subsection is drawn as Forza's shaders
draw its technique, from what is decoded of them (research/forza-materials.md,
research/lights.md "Lamp glass"):

- the texel from the technique's atlas (nodamage, Lights, wheel.xds) through
  m_UVOffsetScale, wrapped like Forza's sampler; one flat colour for swatches;
- paint techniques: texel x alpha + paint x (1 - alpha), the paint the built car's
  palette gives (the same paint in both renders);
- trims: the texel times the technique's texel scale (PristinePaintScale or
  PaintScale), specular and reflection from `forza_materials.gloss_params` (the
  car's ShaderSettings.xml over the shared Normal.xml defaults);
- chrome: the texel times ChromeDiffuseScale under a reflection of CubeScale;
- alpha-tested badges and emblems cut at alpha 128;
- lamps: their Lights texel, glowing in the lit modes by the texel alpha (Forza's glow
  mask) times the lamp function's colour at DSF's level for that function;
- lamp glass: see-through, opacity max(texel alpha, GlassColor.w) capped by
  GlassMaxOpacity, tinting what is behind (lights_gls_* glass glows by its alpha);
- windows: DSF's window glass (the same glass in both renders).

Every triangle carries its origin (section, technique, region, lamp function):
`ForzaScene.origins`, indexed by `scene.tag`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from dsfforza.game import car_render as cr

# Forza's specular in this renderer's light: what dsf_materials.gloss_material maps a
# Forza SpecularAmount onto (GLOSS_SPECULAR_SCALE) times DSF's (car_render.DSF_SPECULAR),
# and its reflection the same way (GLOSS_REFLECTION_SCALE): a gloss trim converted
# within the converter's caps draws alike in both renders.
FORZA_SPECULAR = cr.DSF_SPECULAR * 0.01
FORZA_REFLECTION = 0.5
CHROME_DEFAULTS = (2.0, 0.1)  # CubeScale, ChromeDiffuseScale (Shared.zip Normal.xml, the chrome techniques)
# The built car's paint is the shared atlas's plain texel (red about 240) times the
# palette colour (car_render._base): the Forza paint takes the same factor.
PAINT_TEXEL = float(cr._srgb_to_linear(240 / 255))
TILING_FLAT = 8.0  # |UV scale| above this on the Lights atlas: flat too
TILING_COLOUR = (0.006, 0.006, 0.007)  # linear
TYRE_COLOUR = (0.018, 0.018, 0.018)
LAMP_SHADING = cr.Shading(1.0, 0.04, 0.5, 1.0, 4.0 * FORZA_SPECULAR, 128.0, True)
GLASS_SHADING = cr.Shading(0.0, 0.06, 0.3, 1.0, 2.5, 300.0)  # car_render's window glass

# Lamp glass (research/lights.md, "Lamp glass"): shared GlassColor (rgb, w), GlassMaxOpacity
# and PaintScale per technique; cars override GlassMaxOpacity.
LENS_DEFAULTS = {
    "detail_glass_red": ((0.25, 0.0, 0.0), 0.0, 0.7, 0.2),
    "detail_glass_clear": ((0.2, 0.2, 0.2), 0.6, 0.7, 0.2),
    "detail_glass_amber": ((0.0, 0.0, 0.0), 0.8, 0.7, 0.2),
    "lights_glass": ((0.5, 0.5, 0.5), 0.0, 1.0, 1.0),
    "glass": ((0.0, 0.0, 0.0), 0.0, 1.0, 1.0),  # no texture: GlassColor (0) and the reflection (#160)
    "lights_gls_noemit": ((0.5, 0.5, 0.5), 0.0, 1.0, 1.0),
}
# The black frit round the windows: on the atlas's white body area (alpha 0) in its UVs,
# drawn as gloss black (no texel; the converter's shared trim).
UNTEXTURED_BLACK = ("black_glass", "black_glass_breakable")
UNTEXTURED_GLASS = ("glass",)  # plain glass shaders: the environment only, opacity GlassColor.w and Fresnel
LAMP_GLASS_PREFIX = "lights_gls_"  # lamp glass that glows: opacity GlassMaxOpacity, PaintScale 0.7
LAMP_SECTION = re.compile(r"^(head|tail|fog|brake|indicator|signal|marker|reverse)\w*|^glass[lr]?(hl|tl)", re.I)

# Regions of the comparison (render_compare.REGIONS).
BODY, TRIMS, CHROME, GLASS, PLATES, BADGES, WHEELS, FRONT_LAMPS, REAR_LAMPS = (
    "body",
    "trims",
    "chrome",
    "glass",
    "plates",
    "badges",
    "wheels",
    "front_lamps",
    "rear_lamps",
)
COCKPIT, GAUGES, CABIN, DISPLAYS = "cockpit", "gauges", "cabin", "displays"
CABIN_SECTION = re.compile(r"^(interior|seat|steering_wheel|dash|cagerace|driver)", re.I)


@dataclass(frozen=True)
class Origin:
    """Where a reference triangle came from."""

    section: str
    technique: str
    region: str
    function: str = ""  # lamps: lights.TECHNIQUE_FUNCTION's function, "lens", "cover", "reflector", "lamp"
    role: str = ""


@dataclass
class ForzaScene:
    scene: cr.Scene
    origins: list[Origin]
    lod: int
    notes: dict = field(default_factory=dict)


def lamp_function(technique: str, section: str, family: str | None) -> str | None:
    """The lamp part a technique is (None: not a lamp part)."""
    from dsfforza.convert import lights

    t = technique
    entry = lights.TECHNIQUE_FUNCTION.get(t)
    if entry is not None:
        return entry[0]
    if t in lights.REFLECTORS:
        return "reflector"
    if t in lights.LAMP_COVERS or t.startswith(LAMP_GLASS_PREFIX):
        return "cover"
    if family == "lights":
        return "lamp" if LAMP_SECTION.match(section) or t.startswith("lights_") else "lamp_trim"
    return None


def region_of(section: str, technique: str, role: str, family: str | None, front: bool) -> tuple[str, str]:
    """(region, lamp function) of a body subsection's triangle."""
    from dsfforza.convert import forza_materials as fm

    t = fm.technique_source(technique).lower()
    if CABIN_SECTION.match(section) or t in ("interior", "interior_banner"):
        return CABIN, ""  # seen through the windows only
    if is_window(section, t):
        return GLASS, ""
    function = lamp_function(t, section, family)
    if function is not None and function != "lamp_trim":
        return (FRONT_LAMPS if front else REAR_LAMPS), function
    if t in ("numberplate", "numplate", "numberplate_body"):
        return PLATES, ""
    if "badge" in t or "emblem" in t:
        return BADGES, ""
    if role == fm.PAINT:
        return BODY, ""
    if role == fm.GLASS:
        return GLASS, ""
    if role == fm.CHROME:
        return CHROME, ""
    if role == fm.TYRE:
        return WHEELS, ""
    return TRIMS, ""


def is_window(section: str, technique: str) -> bool:
    """A window drawn with the lamp-cover technique `glass` (FH1 cars' windscreens and side
    windows): the converter keeps those as window glass (dsf_materials.WINDOW_SECTIONS)."""
    from dsfforza.convert import dsf_materials as dm

    return technique.lower() == "glass" and section.lower() in dm.WINDOW_SECTIONS


class _Atlases:
    """Forza atlases as float arrays (h, w, 4), sRGB as stored, per family."""

    def __init__(self, folder: Path, lod0: bool = False):
        from dsfforza.convert import forza_materials as fm

        self.raw = fm.AtlasCache(folder, lod0=lod0)
        self.arrays: dict[str, np.ndarray | None] = {}

    def get(self, family: str | None) -> np.ndarray | None:
        if family is None:
            return None
        if family not in self.arrays:
            try:
                w, h, rgba = self.raw.get(family)
            except (FileNotFoundError, OSError, ValueError):
                self.arrays[family] = None
            else:
                self.arrays[family] = np.frombuffer(bytes(rgba), np.uint8).reshape(h, w, 4).astype(np.float32) / 255
        return self.arrays[family]


def _settings(folder: Path) -> dict:
    from dsfforza.convert import forza_materials as fm

    try:
        return fm.shader_settings(folder)
    except OSError:
        return {}


def _gloss_shading(gloss, base: cr.Shading | None = None) -> cr.Shading:
    if gloss is None:
        return base or cr.Shading(1.0, 0.04, 0.0, 1.0, 0.0, 1.0, True)
    return cr.Shading(
        1.0,
        float(gloss.f0) if gloss.fresnel_scalar is not None else 1.0,
        float(gloss.reflection * FORZA_REFLECTION),
        1.0,
        float(gloss.specular * FORZA_SPECULAR),
        float(max(1.0, gloss.power)),
        True,
    )


class _Materials:
    """One car_render.Material per (technique, family, UV kind, swatch colour)."""

    def __init__(self, folder: Path, paint: cr.Paint, lod0: bool = False, classifier=None, paint_trims: bool = True):
        """paint_trims: the paint shows through the low-alpha texels of the paint-shader
        trims (misc, plastic2...); not in the cockpit, whose PaintColor isn't the body's."""
        from dsfforza.convert import forza_materials as fm

        self.paint_trims = paint_trims
        self.atlases = _Atlases(folder, lod0)
        self.settings = _settings(folder)
        self.paint = paint
        self.classifier = classifier or fm.classify
        self.materials: list[cr.Material] = []
        self.index: dict = {}

    def _own(self, technique: str) -> dict:
        return self.settings.get(technique.lower(), {})

    def get(self, section, sub, wheel: bool = False) -> tuple[int | None, str, str | None]:
        """(material index or None to leave out, role, family) of a subsection."""
        from dsfforza.convert import forza_materials as fm

        classifier = fm.classify_wheel if wheel else self.classifier
        sm = fm.resolve(section, sub, classifier)
        if sm.role == fm.SKIP or sm.kind == "none":
            return None, sm.role, sm.family
        tech = sm.technique
        if is_window(section.name, tech):
            key = ("window",)
            if key not in self.index:
                self.index[key] = len(self.materials)
                self.materials.append(cr.Material("glass", None, np.zeros(3, np.float32), program="glass", name=tech))
            return self.index[key], fm.GLASS, None
        swatch = None
        if sm.kind == "swatch" and sm.family:
            atlas = self.atlases.raw.get(sm.family) if self.atlases.get(sm.family) is not None else None
            swatch = fm.footprint_colour(atlas, sm.bounds) if atlas is not None else None
        # Tiling UVs: the Lights atlas wraps (the Mustang GT 05's bowls sit in tiles -3, 0 and 3,
        # research/lights.md); elsewhere they read a global texture of the shader (tread,
        # carbon, grille mesh) we don't have: one flat dark colour.
        flat = sm.kind == "tiling" and (
            sm.family != fm.LIGHTS or max(abs(sub.uv_transform[1]), abs(sub.uv_transform[3])) > TILING_FLAT
        )
        key = (tech, sm.role, sm.family, sm.kind if sm.kind != "tiling" else ("flat" if flat else "wrap"), swatch)
        if key not in self.index:
            self.index[key] = len(self.materials)
            self.materials.append(self._make(tech, sm, swatch, flat, wheel))
        return self.index[key], sm.role, sm.family

    def _make(self, tech: str, sm, swatch, flat: bool, wheel: bool) -> cr.Material:
        from dsfforza.convert import forza_materials as fm

        t = tech.lower()
        tex = self.atlases.get(sm.family)
        if swatch is not None:
            tex = (np.array(swatch, np.float32) / 255).reshape(1, 1, 4)
        if flat:
            tex = None
        own = self._own(tech)
        grey = np.array(TILING_COLOUR, np.float32)
        m = cr.Material("textured", tex, grey, program=f"forza_{sm.role}", name=tech)
        if sm.role == fm.TYRE:
            m.kind, m.texture, m.colour = "textured", None, np.array(TYRE_COLOUR, np.float32)
            m.shading = cr.Shading(1.0, 0.02, 0.03, 0.1, 0.05 * FORZA_SPECULAR, 8.0, True)
            return m
        if t in UNTEXTURED_BLACK:
            m.texture, m.colour = None, np.array(TILING_COLOUR, np.float32)
            m.shading = cr.Shading(1.0, 0.04, 0.3, 1.0, 2.0 * FORZA_SPECULAR, 128.0, True)
            return m
        if sm.role == fm.GLASS and sm.family != fm.LIGHTS and t not in LENS_DEFAULTS:
            m.kind, m.texture, m.program = "glass", None, "glass"
            return m
        lens = LENS_DEFAULTS.get(t)
        if lens is not None or t.startswith(LAMP_GLASS_PREFIX):
            colour, floor, cap, scale = lens or ((1.0, 1.0, 1.0), 1.0, 1.0, 0.7)
            cap = float(own.get("GlassMaxOpacity", cap))
            if lens is None:  # lights_gls_*: the lamp shaders start the opacity at 1 (GlassMaxOpacity)
                floor = cap
            m.kind = "lens"
            m.lens = cr.Lens(floor=float(floor), cap=cap, tint=1.0)
            if t in UNTEXTURED_GLASS:
                m.texture = None
            m.scale = float(own.get("PaintScale", scale)) * (max(colour) if lens is not None and max(colour) else 1.0)
            m.shading = cr.Shading(1.0, 0.06, 0.3, 1.0, 2.0 * FORZA_SPECULAR * 4, 200.0, True)
            m.emissive = lens is None
            m.program = "forza_lens"
            return m
        gloss = fm.gloss_params(self.settings, tech)
        if sm.role == fm.PAINT:
            base = cr.SHADING["metallic" if self.paint.metallic else "paint"]
            m.kind = "metallic" if self.paint.metallic else "paint"
            m.paint = cr.Paint(
                tuple(c * PAINT_TEXEL for c in self.paint.c2),
                tuple(c * PAINT_TEXEL for c in self.paint.c3),
                self.paint.power,
                self.paint.bias,
                self.paint.metallic,
            )
            m.shading = cr.Shading(base.diffuse, base.f0, base.reflect, base.gloss, base.highlight, base.power)
            m.paint_blend, m.texture = tex is not None, tex
            m.scale = fm.texel_scale(self.settings, tech) if tex is not None else 1.0
            if t.startswith("body") or tex is None:
                m.packed = False
            # _base draws a paint kind from its texture as tex.r x lerp(1, paint, tex.b);
            # a Forza paint technique's texel is a colour: route it through "textured".
            if tex is not None:
                m.kind = "textured"
            m.alpha_test = t in fm.ALPHA_TESTED
            return m
        if sm.role == fm.CHROME or (wheel and fm.chrome_diffuse_scale(self.settings, tech) is not None):
            cube = float(own.get("CubeScale", CHROME_DEFAULTS[0]))
            diffuse = float(own.get("ChromeDiffuseScale", CHROME_DEFAULTS[1]))
            if wheel:
                diffuse = fm.wheel_paint_scale(tech, self.settings)
                cube = fm.gloss_params(self.settings, tech).cube if fm.gloss_params(self.settings, tech) else 1.0
            m.scale = diffuse
            m.shading = cr.Shading(1.0, 1.0, cube * FORZA_REFLECTION, 1.0, 0.0, 1.0, True)
            m.program = "forza_chrome"
            return m
        if wheel:
            m.scale = fm.wheel_paint_scale(tech, self.settings)
        elif sm.role in (fm.TRIM, fm.TEXTURED) and sm.family != fm.LIGHTS:
            m.scale = fm.texel_scale(self.settings, tech)
        if sm.family == fm.LIGHTS:
            m.shading = LAMP_SHADING
            m.emissive = True
            m.program = "forza_lamp"
            return m
        m.shading = _gloss_shading(gloss)
        m.alpha_test = t in fm.ALPHA_TESTED
        if self.paint_trims and t in fm.PAINT_TECHNIQUES and tex is not None and swatch is None:
            m.paint_blend = True
            m.paint = self.paint
        return m


def _triangles(section, sub, point, normal):
    """(pos (n, 3, 3), nrm (n, 3, 3), raw uv (n, 3, 2), ao (n, 3)) of a subsection."""
    from dsfforza.convert import forza2dsf

    pool = section.vertices_for(sub)
    tris = np.array(sub.triangles(), np.int64).reshape(-1, 3)
    if not len(tris):
        z = np.zeros((0, 3, 3), np.float32)
        return z, z, np.zeros((0, 3, 2), np.float32), np.zeros((0, 3), np.float32)
    used = np.unique(tris)
    remap = {int(v): i for i, v in enumerate(used)}
    pos = np.array([point(pool[i].position) for i in used], np.float32)
    nrm = np.array([normal(pool[i].normal) for i in used], np.float32)
    x_off, x_scale, y_off, y_scale = sub.uv_transform[:4]
    uv = np.array([(pool[i].uv0[0] * x_scale + x_off, pool[i].uv0[1] * y_scale + y_off) for i in used], np.float32)
    ao = np.array([forza2dsf.vertex_sky(pool[i], pool, normal(pool[i].normal)) for i in used], np.float32)
    idx = np.vectorize(remap.__getitem__)(tris)
    return pos[idx], nrm[idx], uv[idx], ao[idx]


_CARS: dict = {}


def load_car(folder: Path, media: str):
    """forza2dsf.load_car, kept for the last car (the body and the cockpit references
    read the same carbin)."""
    from dsfforza.convert import forza2dsf

    key = (str(Path(folder).resolve()), media)
    if key not in _CARS:
        _CARS.clear()
        _CARS[key] = forza2dsf.load_car(Path(folder), media)
    return _CARS[key]


def _placement(placement: dict):
    from dsfforza.convert import forza2dsf

    return forza2dsf.Placement(float(placement["dy"]), float(placement["dz"]))


def body_scene(
    folder: Path, media: str, placement: dict, lod: int, paint: cr.Paint, hubs=None, daytime_heads: bool = True
) -> ForzaScene:
    """The reference scene of the main carbin's vanilla body at `lod`, with the wheel
    model on `hubs` ([(position, quaternion)] where the built car's wheels stand; None:
    no wheels). Lamps light as the converter's lamp plan types them (lights.LampPlan:
    function, side, colour), head lamps as 0x1D with `daytime_heads` (the built car has
    such lamps), else 0x01; how much each texel glows is Forza's alpha."""
    from dsfforza.convert import forza2dsf, lights
    from dsfforza.convert import forza_materials as fm

    car = load_car(Path(folder), media)
    place = _placement(placement)
    mats = _Materials(Path(folder), paint)
    plan = lights.LampPlan.build(
        car.body,
        place,
        float(placement.get("host_axle_mid", 0.0)),
        texel=lights.texel_sampler(fm.AtlasCache(Path(folder))),
    )
    names = [s.name for s in car.body]
    parts, origins, origin_index = [], [], {}
    for section in car.body:
        if forza2dsf.raised_lamp(section.name, names):
            continue  # pop-up lamps: the lowered state (forza2dsf.vanilla_sections)
        for sub in section.subsections:
            if sub.lod != lod:
                continue
            m, role, family = mats.get(section, sub)
            if m is None:
                continue
            pos, nrm, uv, ao = _triangles(section, sub, place.point, place.normal)
            if not len(pos):
                continue
            lamp_type = np.zeros((len(pos), 2), np.int32)  # (type byte, colour byte) per triangle
            for k in range(len(pos)):
                lamp = plan.lamps.get(plan.tri_lamp.get((id(sub), k)))
                if lamp is not None:
                    t = lights.HEAD_DAYTIME if lamp.function == lights.HEAD and daytime_heads else lamp.type
                    lamp_type[k] = (t, lamp.colour)
            parts.append((pos, nrm, uv, ao, m, section.name, sub.material, role, family, lamp_type))
    if not parts:
        raise ValueError(f"{media}: no body triangles at LOD {lod}")
    allpos = np.concatenate([p[0] for p in parts]).reshape(-1, 3)
    mid_z = float((allpos[:, 2].min() + allpos[:, 2].max()) / 2)
    out = []
    for pos, nrm, uv, ao, m, sname, tech, role, family, lamp_type in parts:
        front = pos[:, :, 2].mean(axis=1) > mid_z
        tag = np.zeros(len(pos), np.int64)
        for end in (True, False):
            sel = front == end
            if not sel.any():
                continue
            region, function = region_of(sname, tech, role, family, end)
            key = (sname, tech, region, function, role)
            if key not in origin_index:
                origin_index[key] = len(origins)
                origins.append(Origin(sname, tech, region, function, role))
            tag[sel] = origin_index[key]
        out.append((pos, nrm, uv, ao, m, tag, lamp_type))
    lamp_infos, lamp_of = [], {}
    lamp = []
    for pos, *_rest, m, _tag, lamp_type in out:
        idx = np.full(len(pos), -1, np.int32)
        if mats.materials[m].emissive:
            for key in {tuple(int(v) for v in row) for row in lamp_type if row[0]}:
                if key not in lamp_of:
                    lamp_of[key] = len(lamp_infos)
                    lamp_infos.append(cr.LampInfo(key[0], key[1]))
                idx[(lamp_type[:, 0] == key[0]) & (lamp_type[:, 1] == key[1])] = lamp_of[key]
        lamp.append(idx)
    scene = cr.Scene(
        np.concatenate([o[0] for o in out]),
        np.concatenate([o[1] for o in out]),
        np.concatenate([o[2] for o in out]),
        np.concatenate([o[3] for o in out]),
        np.concatenate([np.full(len(o[0]), o[4], np.int32) for o in out]),
        mats.materials,
        np.zeros(sum(len(o[0]) for o in out), bool),
        np.concatenate(lamp),
        lamp_infos,
        np.concatenate([o[5] for o in out]),
    )
    fs = ForzaScene(scene, origins, lod, {"mid_z": mid_z})
    if hubs:
        fs = join(fs, wheel_scene(car, folder, placement, lod, paint, hubs))
    return fs


def wheel_scene(car, folder: Path, placement: dict, lod: int, paint: cr.Paint, hubs) -> ForzaScene:
    """The Forza wheel model at the body's LOD (forza2dsf.wheel_lod), sized per axle as the
    converter sizes it (front_fit, rear_fit, tyre_centre) and set on each hub."""
    from dsfforza.convert import forza2dsf

    lod = forza2dsf.wheel_lod(car.wheel, lod)
    mats = _Materials(Path(folder), paint)
    centre = tuple(placement.get("tyre_centre", (0.0, 0.0, 0.0)))
    zs = [float(h[0][2]) for h in hubs]
    mid = sum(zs) / len(zs)
    scenes = []
    origin = Origin("wheel", "wheel", WHEELS, "", "wheel")
    for axle in ("front", "rear"):
        fit = (*placement.get(f"{axle}_fit", placement.get("front_fit", (1.0, 1.0))), *centre)
        on = [h for h in hubs if (float(h[0][2]) > mid) == (axle == "front")] or (hubs if axle == "front" else [])
        if not on:
            continue
        pos_l, nrm_l, uv_l, ao_l, mat_l = [], [], [], [], []
        for section in car.wheel:
            for sub in section.subsections:
                if sub.lod != lod or forza2dsf.wheel_role(sub.material) is None:
                    continue
                m, _, _ = mats.get(section, sub, wheel=True)
                if m is None:
                    continue
                pos, nrm, uv, ao = _triangles(
                    section,
                    sub,
                    lambda p, f=fit: forza2dsf.wheel_point(p, f),
                    lambda n, f=fit: forza2dsf.wheel_normal(n, f),
                )
                pos_l.append(pos)
                nrm_l.append(nrm / np.maximum(np.linalg.norm(nrm, axis=2, keepdims=True), 1e-9))
                uv_l.append(uv)
                ao_l.append(np.full(ao.shape, 0.6, np.float32))
                mat_l.append(np.full(len(pos), m, np.int32))
        if not pos_l:
            continue
        pos, nrm = np.concatenate(pos_l), np.concatenate(nrm_l)
        for hub, q in on:
            r = cr._quat_matrix(q)
            n = len(pos)
            scenes.append(
                cr.Scene(
                    pos @ r.T + hub,
                    nrm @ r.T,
                    np.concatenate(uv_l),
                    np.concatenate(ao_l),
                    np.concatenate(mat_l),
                    mats.materials,
                    np.zeros(n, bool),
                    np.full(n, -1, np.int32),
                    [],
                    np.zeros(n, np.int64),
                )
            )
    if not scenes:
        return ForzaScene(_empty(mats.materials), [origin], lod)
    return ForzaScene(cr.Scene.join(scenes), [origin], lod)


def _empty(materials) -> cr.Scene:
    z = np.zeros((0, 3, 3), np.float32)
    return cr.Scene(
        z,
        z,
        np.zeros((0, 3, 2), np.float32),
        np.zeros((0, 3), np.float32),
        np.zeros(0, np.int32),
        materials,
        np.zeros(0, bool),
        np.zeros(0, np.int32),
        [],
        np.zeros(0, np.int64),
    )


def join(a: ForzaScene, b: ForzaScene) -> ForzaScene:
    """One reference scene of two (b's origins renumbered after a's)."""
    b_scene = cr.Scene(**{**b.scene.__dict__, "tag": b.scene.tag + len(a.origins)})
    return ForzaScene(cr.Scene.join([a.scene, b_scene]), a.origins + b.origins, a.lod, {**b.notes, **a.notes})


# --- lamps in the lit modes --------------------------------------------------------------


def forza_lamp_rgb(lamps: list[cr.LampInfo], lighting: cr.Lighting) -> np.ndarray | None:
    """(L, 3) emission colour of each reference lamp in this light: DSF's level for its
    type (car_render.lamp_level). A 0x1D head lamp glows like a 0x01 one: the converter
    draws the texels of those lamps at DAYTIME_GLOW (a fifth) against the type's five
    times higher level (research/lights.md, "Head lamps white plates by day")."""
    if not lamps:
        return None
    out = []
    for lp in lamps:
        level = cr.lamp_level(lp.type, lighting.lamps)
        if lp.type == 0x1D:
            level *= cr.TYPE_BRIGHTNESS[1] / cr.TYPE_BRIGHTNESS[0x1D]
        out.append(np.array(cr.LAMP_COLOURS.get(lp.colour, cr.LAMP_COLOURS[8])) * level)
    return np.array(out, np.float32).reshape(-1, 3)


# --- the cockpit -----------------------------------------------------------------------------

GAUGE_TECHNIQUE = re.compile(r"^(cockpit_)?(z_)?(digital_)?gauge|needle", re.I)
DISPLAY_TECHNIQUE = re.compile(r"^(cockpit_)?(z_)?digital_gauge", re.I)


def cockpit_region(section: str, technique: str, role: str, family: str | None, front: bool) -> tuple[str, str]:
    """(region, lamp function) of a cockpit-carbin triangle: gauges (dials, needles,
    displays), the cabin, or for its outside sections (A-pillars, bonnet, mirrors) the
    body regions."""
    from dsfforza.convert import cockpit
    from dsfforza.convert import forza_materials as fm

    t = fm.technique_source(technique)
    if DISPLAY_TECHNIQUE.match(t):
        return DISPLAYS, ""  # drawn live (Forza's dash scene, DSF's digital dials): not compared
    if GAUGE_TECHNIQUE.match(t) or cockpit.NEEDLE.match(section):
        return GAUGES, ""
    if not fm.COCKPIT_EXTERIOR_SECTIONS.match(section):
        return COCKPIT, ""
    region, function = region_of(section, technique, role, family, front)
    return (COCKPIT if region == CABIN else region), function


def cockpit_scene(
    folder: Path, media: str, placement: dict, paint: cr.Paint, shell_lod: int = 1
) -> tuple[ForzaScene, dict]:
    """The reference of the cockpit view: the cockpit carbin's static sections and its
    needles at rest (as cockpit.fit measures them, the main carbin's cabin standing in for
    a stub), drawn as Forza's cockpit shaders do (forza_materials.classify_cockpit, the
    _LOD0 atlases), and the main carbin's body at `shell_lod` round it. Returns the scene
    and {eye (Forza space)}."""
    from dsfforza.convert import cockpit
    from dsfforza.convert import forza_materials as fm
    from dsfforza.convert import paint as paint_mod

    folder = Path(folder)
    car = load_car(folder, media)
    place = _placement(placement)
    gamedb = paint_mod.find_gamedb(folder)
    sections = cockpit.load_cockpit(folder, media) or []
    cf = cockpit.fit(sections, media, [gamedb] if gamedb else [], folder=folder, main_sections=car.sections)
    static = {
        n: s for n, s in cf.static.items() if n not in cockpit.SKIP_SECTIONS and not cockpit.LIGHT_SECTIONS.search(n)
    }
    for family, poses in cf.needles.items():
        pose = poses.get("mid" if cockpit.PARK_MID.match(family) else "min") or next(iter(poses.values()))
        static[pose.name] = pose
    classifier = fm.classify_cockpit
    if not cf.lod0_atlases:

        def classifier(name):
            role, family = fm.classify_cockpit(name)
            return role, fm.NODAMAGE if family == fm.INTERIOR else family

    mats = _Materials(folder, paint, lod0=cf.lod0_atlases, classifier=classifier, paint_trims=False)
    skip = {n.lower() for n in cf.static} | {n.lower() for n in cf.shell_skip} | {n.lower() for n in static}
    shell = [s for s in car.body if s.name.lower() not in skip and not cockpit.SHELL_SKIP.match(s.name)]
    shell_mats = _Materials(folder, paint)
    lods = sorted({sub.lod for s in shell for sub in s.subsections if sub.lod > 0})
    lod = max((x for x in lods if x <= shell_lod), default=lods[0] if lods else 1)
    groups = [(list(static.values()), 0, mats, cockpit_region), (shell, lod, shell_mats, region_of)]
    parts, origins, origin_index = [], [], {}
    all_materials: list = []
    for secs, at, mm, regioner in groups:
        offset = len(all_materials)
        for section in secs:
            for sub in section.subsections:
                if sub.lod != at:
                    continue
                m, role, family = mm.get(section, sub)
                if m is None:
                    continue
                pos, nrm, uv, ao = _triangles(section, sub, place.point, place.normal)
                if not len(pos):
                    continue
                region, function = regioner(section.name, sub.material, role, family, True)
                key = (section.name, sub.material, region, function, role)
                if key not in origin_index:
                    origin_index[key] = len(origins)
                    origins.append(Origin(section.name, sub.material, region, function, role))
                parts.append((pos, nrm, uv, ao, m + offset, origin_index[key]))
        all_materials += mm.materials
    if not parts:
        raise ValueError(f"{media}: no cockpit triangles")
    n = sum(len(p[0]) for p in parts)
    scene = cr.Scene(
        np.concatenate([p[0] for p in parts]),
        np.concatenate([p[1] for p in parts]),
        np.concatenate([p[2] for p in parts]),
        np.concatenate([p[3] for p in parts]),
        np.concatenate([np.full(len(p[0]), p[4], np.int32) for p in parts]),
        all_materials,
        np.ones(n, bool),
        np.full(n, -1, np.int32),
        [],
        np.concatenate([np.full(len(p[0]), p[5], np.int64) for p in parts]),
    )
    return ForzaScene(scene, origins, 0, {"source": cf.source}), {"eye": cf.eye}
