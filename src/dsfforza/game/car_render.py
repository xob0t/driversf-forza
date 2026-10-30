"""Software render of a converted car for its garage picture (research/gui-assets.md).

The garage picture of a car without an FH1 thumbnail is drawn from the ported
archive: the clean exterior entry (kind 2), the wheel part once per UPVH hub,
and the interior entry (kind 1), which shows through the glass. numpy only:

- every triangle is cut into pixel tiles and tested against the pixel centres
  (`_fragments`); per pixel the nearest fragment wins (z-buffer, 1 / z);
- opaque surfaces first, then up to two glass layers in front of them, each
  shaded from its triangle's perspective-correct barycentrics (deferred);
- materials by DSF material UID (the shared paint, glass, trim, tyre and
  chrome materials) and by the local material's name (Lights, Interior,
  Chassis), with their own textures (local DRSPs and the archive's shared
  SSIC chunk);
- light: a soft studio environment (bright sky, dark floor) seen in
  reflections with Schlick's Fresnel term, a key light from the camera's
  upper left, the vertex sky / AO term (vehicle-model.md) as ambient
  occlusion, and a soft contact shadow on the floor.

The camera matches the vanilla garage pictures (`VANILLA_CAMERA`; measured with
`dsff gui_assets camera`, research/gui-assets.md "Camera").

The same renderer draws the comparison renders of `dsff render_compare`
(research/render-compare.md): `load_scene(..., physical=True)` shades each
material with its DSF program's terms and its own parameters (`dsf_shading`),
gives every triangle its lamp (vertex semantic 9 byte 2 and the UPVH light table)
and loads any entry (the clean exterior, the full entry at one LOD mask, the
interior); `rasterize` keeps which triangle each pixel shows (the ID buffer) and
`shade` lights the same frame by day, with the lamps lit by day, or in the dark
mood (`Lighting`).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dsfforza.formats.vehicle_mesh import child, payload

PAINT_UIDS = {bytes.fromhex("001283358afd1027"), bytes.fromhex("10122b9653b14e2a")}
GLASS_UIDS = {bytes.fromhex("001283369afd1027"), bytes.fromhex("10122b9953b14e2a")}
INTERIOR_GLASS_UIDS = {bytes.fromhex("0012cc8576953429")}  # GLOBAL_Glass_LOD5: interior windows
TRIM_UIDS = {bytes.fromhex("0012833b12fe1027"), bytes.fromhex("10122b9753b14e2a")}
TYRE_UIDS = {bytes.fromhex("00128337b2fd1027"), bytes.fromhex("10122b9b53b14e2a")}
CHROME_UIDS = {bytes.fromhex("a012f01306d57a2a"), bytes.fromhex("00128338d7fd1027")}
SHARED_CHASSIS_TEXTURE = bytes.fromhex("00122b73d1fa1227")  # paint, trim and chrome sample it (lights.md)
PAINT_BLOCK_UV = (0.848, 0.712)  # the (240, 192, 240) paint block of the shared atlas


@dataclass(frozen=True)
class Camera:
    """yaw: degrees from straight ahead towards the car's right side; pitch:
    degrees above the horizon; distance: eye to the car's centre in car lengths
    (smaller = stronger perspective)."""

    yaw: float
    pitch: float
    distance: float
    # A camera at a point instead (the cockpit view): car-space eye, view direction and
    # horizontal field of view in degrees; yaw, pitch and distance are then unused.
    eye: tuple[float, float, float] | None = None
    look: tuple[float, float, float] | None = None
    fov: float = 70.0


VANILLA_CAMERA = Camera(yaw=36.5, pitch=13.0, distance=1.3)  # median of 24 vanilla pictures (gui-assets.md, "Camera")


@dataclass
class Shading:
    """How a material class is lit (linear colour space)."""

    diffuse: float  # scale of the base colour under the lights
    f0: float  # Fresnel reflectance at normal incidence
    reflect: float  # environment reflection strength (times Fresnel)
    gloss: float  # 0 = the environment blurred to the normal, 1 = a mirror
    highlight: float  # key-light specular strength
    power: float  # key-light specular exponent
    # Compare renders (dsf_shading, harness/forza_render.py): Phong's (R.L)^power, as
    # DSF's and Forza's programs have it, instead of Blinn's (N.H)^power; the specular and
    # reflection times a texel channel ("g" green, "a" alpha; DSF's programs mask them so);
    # Schlick's Fresnel power.
    phong: bool = False
    mask: str | None = None  # the reflection's
    fresnel_power: float = 5.0
    spec_mask: str | None = None  # the specular's


# Tone calibrated against the vanilla garage pictures (research/gui-assets.md, "Tone"):
# renders of 10 vanilla cars with saturated paint, from the vanilla archive with the
# palette entry of the picture's hue, matched to their pictures by the brightness
# (HSV value) and saturation percentiles and the share of white highlight pixels of
# the car. The pictures show deep, saturated paint with soft highlights and almost
# no environment sheen: reflections are a tenth to a third of the first renderer's,
# key light and sky ambient a little higher.
SHADING = {
    "paint": Shading(1.0, 0.05, 0.15, 1.0, 0.45, 150.0),
    "metallic": Shading(1.0, 0.08, 0.15, 1.0, 0.6, 80.0),
    "trim": Shading(1.0, 0.03, 0.1, 0.4, 0.15, 20.0),
    "tyre": Shading(1.0, 0.02, 0.03, 0.1, 0.05, 8.0),
    "chrome": Shading(0.15, 0.75, 1.0, 1.0, 2.0, 200.0),
    "lights": Shading(1.0, 0.06, 0.3, 1.0, 2.0, 200.0),
    "wheel": Shading(0.9, 0.2, 0.24, 0.7, 0.8, 60.0),
    "textured": Shading(1.0, 0.04, 0.18, 0.8, 0.5, 60.0),
    "interior": Shading(0.85, 0.02, 0.045, 0.3, 0.1, 10.0),
    "glass": Shading(0.0, 0.06, 0.3, 1.0, 2.5, 300.0),
}
GLASS_TINT = np.array([0.55, 0.6, 0.64])  # transmitted light through one glass layer (linear)
GLASS_HAZE = np.array([0.012, 0.014, 0.018])  # glass's own colour seen on top

# Studio environment, linear RGB (calibrated, above: a quarter of the first renderer's).
ENV_ZENITH = np.array([0.25, 0.25, 0.25])
ENV_HORIZON = np.array([0.04, 0.0425, 0.0475])
ENV_GROUND = np.array([0.015, 0.015, 0.016])
ENV_FLOOR_FAR = np.array([0.06, 0.06, 0.065])
SOFTBOX_DIR = np.array([0.45, 0.75, -0.5]) / np.linalg.norm([0.45, 0.75, -0.5])
SOFTBOX_EDGE = (0.86, 0.95)  # cosines: dark outside the first, full inside the second
SOFTBOX = 0.45
KEY_LIGHT = (-0.5, 0.55, 0.55)  # towards the light, car frame before the camera turn (see _light)
KEY_POWER = 0.96
SKY_AMBIENT, GROUND_AMBIENT = 0.62, 0.007
EXPOSURE = 0.85
SHADOW_ALPHA = 0.75


# --- the scene -----------------------------------------------------------------------


@dataclass
class Material:
    kind: str  # a SHADING key
    texture: np.ndarray | None  # (h, w, 4) float32 0..1, sRGB as stored
    colour: np.ndarray  # linear RGB, the base without a texture (paint: the paint's colour 2)
    packed: bool = False  # a shared SSIC texture: channel-packed, red is the grey level
    paint: Paint | None = None  # paint kinds: the palette entry (colour by the view angle)
    # Compare renders only (the garage picture leaves these as they are):
    shading: Shading | None = None  # instead of SHADING[kind]
    scale: float = 1.0  # the texel colour times this (linear light)
    paint_blend: bool = False  # texel x alpha + paint x (1 - alpha): Forza's paint shaders
    alpha_test: bool = False  # texels with alpha under one half are holes
    emissive: bool = False  # glows by texel alpha x the triangle's lamp colour (lit modes)
    lens: Lens | None = None  # kind "lens": see-through lamp glass, opacity from the texel
    program: str = ""  # what draws it, for the reports: a DSF material class or a Forza role
    name: str = ""  # the DSF material's name or the Forza technique
    uid: bytes = b""  # the DSF material's UID


@dataclass(frozen=True)
class Lens:
    """See-through lamp glass (kind "lens"): opacity min(max(texel alpha x alpha,
    floor), cap); what is behind is filtered by (1 - opacity) + opacity x tint x texel."""

    floor: float = 0.0
    cap: float = 1.0
    alpha: float = 1.0
    tint: float = 1.0


PAINT_KINDS = ("paint", "metallic")
TRANSPARENT_KINDS = ("glass", "lens")


@dataclass
class LampInfo:
    """One lamp a scene's triangles light with: DSF's light-table type (flags b3) and
    colour (b2) bytes, or a Forza lamp function (harness/forza_render.py)."""

    type: int = 0
    colour: int = 8
    function: str = ""


@dataclass
class Scene:
    """Triangles (T): positions (T, 3, 3), normals (T, 3, 3), UV0 (T, 3, 2),
    sky / AO term (T, 3), material index (T,); inside (T,): from the interior entry.
    Compare renders add lamp (T,): index into lamps, -1 for none; tag (T,): an index
    the caller keeps its own per-triangle data under (the Forza origin, render_compare)."""

    pos: np.ndarray
    nrm: np.ndarray
    uv: np.ndarray
    ao: np.ndarray
    mat: np.ndarray
    materials: list[Material]
    inside: np.ndarray | None = None
    lamp: np.ndarray | None = None
    lamps: list[LampInfo] | None = None
    tag: np.ndarray | None = None
    info: dict | None = None  # what load_scene read: hubs, entry kinds, paint

    @property
    def glass(self) -> np.ndarray:
        kinds = np.array([m.kind == "glass" for m in self.materials])
        return kinds[self.mat]

    @property
    def transparent(self) -> np.ndarray:
        kinds = np.array([m.kind in TRANSPARENT_KINDS for m in self.materials])
        return kinds[self.mat]

    def subset(self, keep: np.ndarray) -> Scene:
        """The triangles where keep (T,) is true (materials and lamps shared)."""

        def cut(a):
            return None if a is None else a[keep]

        return Scene(
            self.pos[keep],
            self.nrm[keep],
            self.uv[keep],
            self.ao[keep],
            self.mat[keep],
            self.materials,
            cut(self.inside),
            cut(self.lamp),
            self.lamps,
            cut(self.tag),
            self.info,
        )

    @staticmethod
    def join(scenes: list[Scene]) -> Scene:
        """One scene of several (their materials and lamps appended; tags kept)."""
        materials, lamps, parts = [], [], []
        for s in scenes:
            m0, l0 = len(materials), len(lamps)
            materials += s.materials
            lamps += s.lamps or []
            n = len(s.mat)
            lamp = s.lamp if s.lamp is not None else np.full(n, -1, np.int32)
            parts.append(
                (
                    s.pos,
                    s.nrm,
                    s.uv,
                    s.ao,
                    s.mat + m0,
                    s.inside if s.inside is not None else np.zeros(n, bool),
                    np.where(lamp >= 0, lamp + l0, -1).astype(np.int32),
                    s.tag if s.tag is not None else np.full(n, -1, np.int64),
                )
            )
        cols = list(zip(*parts))
        return Scene(
            *(np.concatenate(c) for c in cols[:5]),
            materials,
            np.concatenate(cols[5]),
            np.concatenate(cols[6]),
            lamps,
            np.concatenate(cols[7]),
            scenes[0].info if scenes else None,
        )


_SHARED: dict = {}
SHARED_KEEP = 2  # archives whose shared materials stay read (render_compare diff reads two)


def _decode_texture(stream, texc) -> np.ndarray:
    from dsfforza.convert import dsf_materials as dm

    w, h, px = dm.decode_dds(payload(stream, child(texc, "DXTC")))
    return (np.frombuffer(bytes(px), np.uint8).reshape(h, w, 4).astype(np.float32)) / 255


def _shared(archive: Path, stream, entries) -> tuple[dict, dict]:
    """The archive's shared SSIC materials and textures (uid -> info / lazily decoded
    texture entry), read once per archive file version."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats.chnk import walk

    st = archive.stat()
    key = (str(archive.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _SHARED:
        while len(_SHARED) >= SHARED_KEEP:
            del _SHARED[next(iter(_SHARED))]
        materials, textures = {}, {}
        ssic = next(e for e in entries if e.tag == "SSIC")
        for _, e in walk(ssic.children):
            if e.tag == "TEXC":
                textures[payload(stream, child(e, "UIDC"))[:8]] = e
            elif e.tag == "MACO":
                info = dm.material_info(stream, e)
                materials[info["uid"]] = info
        _SHARED[key] = (materials, textures, {})
    return _SHARED[key]


def _elements(vb) -> dict:
    from dsfforza.formats import vehicle_mesh as vm

    raw = np.frombuffer(vb.data, np.uint8)[: vb.count * vb.stride].reshape(vb.count, vb.stride)
    out = {}
    for e in vb.elements:
        fmt, size = vm.COMPONENT_TYPES[e.type]
        dtype = {"h": "<i2", "B": "u1", "e": "<f2"}[fmt]
        block = np.ascontiguousarray(raw[:, e.offset : e.offset + e.components * size])
        out.setdefault(e.semantic, block.view(dtype).reshape(vb.count, e.components).astype(np.float32))
    return out


def _decode_normals(packed: np.ndarray) -> np.ndarray:
    polar = (packed[:, 0] - 128) / 127 * math.pi
    azimuth = (packed[:, 1] - 128) / 128 * math.pi
    return np.stack((np.sin(polar) * np.sin(azimuth), np.cos(polar), np.sin(polar) * np.cos(azimuth)), axis=1).astype(
        np.float32
    )


def _quat_matrix(q) -> np.ndarray:
    x, y, z, w = q
    n = math.sqrt(x * x + y * y + z * z + w * w) or 1.0
    x, y, z, w = x / n, y / n, z / n, w / n
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float32,
    )


def _submodel_triangles(mesh, sub, cache: dict):
    """(pos, nrm, uv, ao) per triangle corner of one submodel: (n, 3, ...)."""
    vb = mesh.vertex_buffers[sub.vertex_buffer]
    if vb.uid not in cache:
        cache[vb.uid] = _elements(vb)
    el = cache[vb.uid]
    idx = np.frombuffer(mesh.index_buffers[sub.index_buffer], "<u2")[
        sub.first_index : sub.first_index + sub.index_count
    ].astype(np.int64)
    idx = idx[: len(idx) // 3 * 3] + sub.base_vertex
    idx = idx[(idx < vb.count).reshape(-1, 3).all(axis=1).repeat(3)]
    p = el[0][idx]
    pos = (p[:, :3] / 4096).reshape(-1, 3, 3)
    ao = (p[:, 3] / 4096 if p.shape[1] > 3 else np.full(len(idx), 0.5, np.float32)).reshape(-1, 3)
    nrm = _decode_normals(el[5][idx]).reshape(-1, 3, 3) if 5 in el else None
    uv = el[8][idx, :2].reshape(-1, 3, 2) if 8 in el else np.zeros((len(idx) // 3, 3, 2), np.float32)
    return pos, nrm, uv, ao


def _fades_in(mesh, sub, cache: dict) -> bool:
    """Whether every vertex a submodel draws has vertex semantic 9 byte 3 = 2: the wheel's
    speed-blur disc (GLOBAL_Wheel_Blur_SpokeDisc and the ports' copies of it), whose
    programs fade it in with the wheel's speed (opacity 2.5 b - 0.15; SSIC GPCH 21 to 40),
    so it is invisible at rest (issue #221). Byte 3 = 1 is always drawn, 0 fades out."""
    vb = mesh.vertex_buffers[sub.vertex_buffer]
    if vb.uid not in cache:
        cache[vb.uid] = _elements(vb)
    s9 = cache[vb.uid].get(9)
    if s9 is None or s9.shape[1] < 4:
        return False
    idx = np.frombuffer(mesh.index_buffers[sub.index_buffer], "<u2")[
        sub.first_index : sub.first_index + sub.index_count
    ].astype(np.int64)
    idx = idx + sub.base_vertex
    idx = idx[idx < vb.count]
    return bool(len(idx)) and bool((s9[idx, 3] == 2).all())


def _face_normals(pos: np.ndarray) -> np.ndarray:
    n = np.cross(pos[:, 1] - pos[:, 0], pos[:, 2] - pos[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    return np.repeat(n[:, None], 3, axis=1).astype(np.float32)


@dataclass(frozen=True)
class Paint:
    """A palette entry as the paint shader uses it (lights.md, "What the paint
    shader does"): colour 2 mixed toward colour 3 by bias + (1 - bias) (1 - n.v)^power,
    linear RGB. Most vanilla entries are a dark colour 2 with a lighter colour 3 at
    grazing angles only (power 5 to 15); a flat colour has colour 2 = colour 3."""

    c2: tuple[float, float, float]
    c3: tuple[float, float, float]
    power: float = 1.0
    bias: float = 0.0
    metallic: bool = False

    @classmethod
    def flat(cls, srgb: tuple[int, int, int], metallic: bool = False) -> Paint:
        lin = tuple(float(_srgb_to_linear(c / 255)) for c in srgb)
        return cls(lin, lin, 1.0, 0.0, metallic)

    def at(self, ndv: np.ndarray) -> np.ndarray:
        """(N, 3) linear paint colour at the cosines ndv between normal and view."""
        f = self.bias + (1 - self.bias) * (1 - np.clip(ndv, 0, 1)) ** self.power
        c2, c3 = np.array(self.c2, np.float32), np.array(self.c3, np.float32)
        return c2[None] + f[:, None] * (c3 - c2)[None]


DEFAULT_PAINT_SRGB = (150, 20, 20)  # a car without a palette


def picture_paint(c2, c3, power: float, bias: float, metallic: bool) -> Paint:
    """The palette entry as the vanilla garage pictures show it. An entry with a
    fresnel power of 1 or more (the usual dark colour 2, lighter colour 3 at the
    edges: the Volt's red) is mixed by the view angle as the shader does; the
    pictures show its colour 2 with lighter edges. Taken literally, an entry
    with a low power or a high bias shows colour 3 almost everywhere, near black
    for most of them, but the vanilla pictures of such colours (370Z blue, power
    0.25; Cobra red, 0.1) show colour 2 facing the camera, darker toward the
    edges: such an entry is drawn with the power raised to LOW_POWER_FLOOR and
    the bias lowered to LOW_POWER_BIAS."""
    if power < LOW_POWER_FLOOR or bias > LOW_POWER_BIAS:
        power, bias = max(power, LOW_POWER_FLOOR), min(bias, LOW_POWER_BIAS)
    return Paint(tuple(c2), tuple(c3), power, bias, metallic)


LOW_POWER_FLOOR, LOW_POWER_BIAS = 2.0, 0.05


def paint_colour(stream, full) -> Paint:
    """The palette's most colourful entry (vanilla pictures rarely show black or
    grey), chosen by the colour it mostly shows (paint.apparent)."""
    from dsfforza.convert import paint

    spss = paint.palette_set(stream, full)
    if spss is None or not spss.children:
        return Paint.flat(DEFAULT_PAINT_SRGB)
    options = []
    for e in spss.children:
        params = paint.spsp_params(stream, e)
        colour = tuple(int(255 * paint.linear_to_srgb(x)) for x in paint.apparent(params))
        metallic = "g_vec4TintColour1_Alpha" in params and paint.is_metallic(params)
        try:
            entry = picture_paint(
                tuple(params["g_vec3TintColour2"][:3]),
                tuple(params["g_vec3TintColour3"][:3]),
                float(params.get("g_fresnelpower", 1.0)),
                float(params.get("g_fresnelbias", 0.0)),
                metallic,
            )
        except (KeyError, TypeError):
            entry = Paint.flat(colour, metallic)
        options.append((colour, entry))
    return max(options, key=lambda o: (max(o[0]) - min(o[0]), -abs(sum(o[0]) / 3 - 128)))[1]


def _rest_hubs(stream, full, hubs: list) -> list:
    """The hubs where the game draws the wheels of a car at rest: each axle's above its
    hub-table position by the engine's static offset (wheel_clearance.rest_offsets; the
    converter lowers the table by that much, forza2dsf.settle_wheels)."""
    from dsfforza.build import handling
    from dsfforza.convert import wheel_clearance

    if len(hubs) < 2:
        return hubs
    params = handling.VO3D.parse(payload(stream, child(child(full, "NONR"), "VO3D"))).params
    zs = [float(hub[2]) for hub, _ in hubs]
    mid = sum(zs) / len(zs)
    front = [z for z in zs if z > mid] or [max(zs)]
    rear = [z for z in zs if z <= mid] or [min(zs)]
    up = wheel_clearance.rest_offsets(params, sum(front) / len(front), sum(rear) / len(rear))
    return [(hub + np.float32([0, up["front" if hub[2] > mid else "rear"], 0]), q) for hub, q in hubs]


def load_scene(
    archive: Path,
    spool_id: int,
    entries=None,
    interior: bool = True,
    entry: str = "exterior",
    mask: int | None = None,
    physical: bool = False,
) -> Scene:
    """The clean exterior (kind 2) with its wheel part on every UPVH hub, and the
    interior (kind 1) without its glass and gauge needles.

    entry "full": the full entry (kind 0) instead, part 0 and the wheel parts at LOD
    mask `mask` (default FULL_MASK; the per-panel damage parts repeat part 0);
    "interior": the interior entry alone, with its glass and needles (the cockpit view).
    physical: materials as their DSF programs draw them (dsf_shading) and every
    triangle's lamp (Scene.lamp, from vertex semantic 9 byte 2 and the full entry's
    light table), for render_compare."""

    archive = Path(archive)
    with archive.open("rb") as stream:
        return _load_scene(archive, stream, spool_id, entries, interior, entry, mask, physical)


FULL_MASK = 4  # the full entry's finest detail LOD (masks 4, 8, 16; 1 is the shadow)
# DSF programs' sun specular per unit of g_hSpecularIntensity (and of the fixed terms of
# the shared programs), in this renderer's light. harness/forza_render.FORZA_SPECULAR is
# this times dsf_materials.GLOSS_SPECULAR_SCALE, so a converted gloss trim draws as its
# Forza technique does (research/render-compare.md, "Shading").
DSF_SPECULAR = 25.0


def _marp_floats(stream, maco) -> dict[str, float]:
    """A MACO's float parameters (type 1; the first pass that has each)."""
    from dsfforza.convert import dsf_materials as dm

    out = {}
    for marc in maco.children:
        if marc.tag != "MARC":
            continue
        for p in marc.children:
            if p.tag == "MARP":
                param = dm.parse_marp(dm.current(stream, p))
                if param.type == 1 and param.name not in out:
                    out[param.name] = struct.unpack_from("<f", param.value)[0]
    return out


def dsf_shading(program: str, params: dict[str, float] | None = None) -> Shading:
    """How a DSF material program lights a texel (research/forza-materials.md, "Gloss":
    the vanilla programs disassembled): light x texel plus

    - trim (GLOBAL_Chassis_Simple): 0.01 x tex.G x (R.V)^7.5, no reflection;
    - chassis (<car>_Chassis, the interior's): 0.03 x tex.a x (R.V)^24;
    - chrome: tex.G x (R.V)^150 + env x tex.G, alike at every angle;
    - tyre: tex.G x (R.V)^30 + env x tex.G x 0.5 x max(0.2, 1 - N.V);
    - lights (<car>_Lights and its gloss copies): g_hSpecularIntensity x
      (R.V)^g_hSpecularPower + env x tex.G x g_hReflectionAmount x Fresnel
      (g_hFresnelPower, g_hReflectionFresnelBias), plus tex.a x the lamp's colour."""
    if program == "trim":
        return Shading(1.0, 0.0, 0.0, 1.0, 0.01 * DSF_SPECULAR, 7.5, True, None, 5.0, "g")
    if program == "chrome":
        return Shading(1.0, 1.0, 1.0, 1.0, DSF_SPECULAR, 150.0, True, "g", 5.0, "g")
    if program == "tyre":
        return Shading(1.0, 0.2, 0.5, 1.0, DSF_SPECULAR, 30.0, True, "g", 1.0, "g")
    if program in ("lights", "gloss"):
        p = params or {}
        return Shading(
            1.0,
            float(min(1.0, max(0.0, p.get("g_hReflectionFresnelBias", 0.0)))),
            float(max(0.0, p.get("g_hReflectionAmount", 1.0))),
            1.0,
            float(max(0.0, p.get("g_hSpecularIntensity", 1.0))) * DSF_SPECULAR,
            float(max(1.0, p.get("g_hSpecularPower", 150.0))),
            True,
            "g",
            float(max(0.5, p.get("g_hFresnelPower", 1.0))),
        )
    return Shading(1.0, 0.0, 0.0, 1.0, 0.03 * DSF_SPECULAR, 24.0, True, None, 5.0, "a")  # chassis, interior


def _submodel_s9(mesh, sub, cache: dict) -> np.ndarray | None:
    """Vertex semantic 9 per triangle corner of one submodel (n, 3, 4), in
    _submodel_triangles' order; None without it."""
    vb = mesh.vertex_buffers[sub.vertex_buffer]
    if vb.uid not in cache:
        cache[vb.uid] = _elements(vb)
    el = cache[vb.uid]
    if 9 not in el:
        return None
    idx = np.frombuffer(mesh.index_buffers[sub.index_buffer], "<u2")[
        sub.first_index : sub.first_index + sub.index_count
    ].astype(np.int64)
    idx = idx[: len(idx) // 3 * 3] + sub.base_vertex
    idx = idx[(idx < vb.count).reshape(-1, 3).all(axis=1).repeat(3)]
    return el[9][idx].reshape(-1, 3, el[9].shape[1])


def light_table(stream, full) -> dict[int, LampInfo]:
    """Light byte (light id + 1) -> its lamp, from the full entry's UPVH light table."""
    from dsfforza.convert import lights

    out = {}
    for row in lights.read_table(payload(stream, child(child(full, "NONR"), "UPVH"))):
        out[row["id"] + 1] = LampInfo(row["flags"] >> 24, (row["flags"] >> 16) & 0xFF)
    return out


def _load_scene(
    archive: Path, stream, spool_id: int, entries, interior: bool, entry="exterior", mask=None, physical=False
) -> Scene:
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import upvh as uv
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.game.gui_assets import _root_entries

    entries = entries if entries is not None else _root_entries(archive, stream)
    kinds = uv.entries_for(stream, entries, spool_id, None)
    index = {k: entries.index(v) for k, v in kinds.items()}
    shared_mats, shared_tex, decoded = _shared(archive, stream, entries)
    textures, infos = {}, {}
    for k in kinds:
        for e in child(child(kinds[k], "MDXN"), "DRSP").children:
            if e.tag == "TEXC":
                textures[payload(stream, child(e, "UIDC"))[:8]] = _decode_texture(stream, e)
            elif e.tag == "MACO":
                info = dm.material_info(stream, e)
                if physical:
                    info["floats"] = _marp_floats(stream, e)
                infos[info["uid"]] = info

    def texture_of(uid: bytes):
        info = infos.get(uid) or shared_mats.get(uid)
        if not info or not info["textures"]:
            return None, False
        t = next(iter(info["textures"]))
        if t in textures:
            return textures[t], False
        if t in shared_tex:
            if t not in decoded:
                decoded[t] = _decode_texture(stream, shared_tex[t])
            return decoded[t], True
        return None, False

    car_paint = paint_colour(stream, kinds[0])
    metallic = car_paint.metallic
    h = uv.parse(payload(stream, child(child(kinds[0], "NONR"), "UPVH")))
    hubs = []
    for i in range(h.counts["wheels"]):
        o = h.offsets["wheels"] + i * 0x20
        hubs.append(
            (np.array(struct.unpack_from("<3f", h.data, o), np.float32), struct.unpack_from("<4f", h.data, o + 16))
        )
    hubs = _rest_hubs(stream, kinds[0], hubs)
    # Which hubs each wheel part goes on: the wheel records' LOD links (hosts with front
    # and rear wheel parts, forza2dsf.split_rear_wheel); a part no record names, all.
    # A link is (full entry part, exterior part).
    links = h.lod_links()
    wheel_kind = 0 if entry == "full" else 2
    part_ids = uv.modl_part_uids(payload(stream, child(child(kinds[wheel_kind], "NONR"), "MODL")))
    part_hubs: dict[int, list[int]] = {}
    for r in h.records:
        link = links[r.lod_link][1 if wheel_kind == 2 else 0] if 0 <= r.lod_link < len(links) else None
        if r.type == 2 and 0 <= r.wheel < len(hubs) and link in part_ids:
            part_hubs.setdefault(part_ids.index(link), []).append(r.wheel)
    paint_lin = np.array(car_paint.c2, np.float32)
    lamp_of = light_table(stream, kinds[0]) if physical else {}

    materials: list[Material] = []
    mat_index: dict = {}

    def material(uid: bytes, inside: bool, wheel: bool) -> int:
        info = infos.get(uid) or shared_mats.get(uid) or {"path": ""}
        name = info["path"].lower().replace("\\", "/").rsplit("/", 1)[-1]
        tex, packed = texture_of(uid)
        if uid in PAINT_UIDS:
            kind = "metallic" if metallic else "paint"
        elif uid in GLASS_UIDS or uid in INTERIOR_GLASS_UIDS:
            kind, tex = "glass", None
        elif uid in TRIM_UIDS:
            kind = "trim"
        elif uid in TYRE_UIDS:
            kind = "tyre"
        elif uid in CHROME_UIDS:
            kind, tex = "chrome", None
        elif inside:
            kind = "interior"
        elif "_lights" in name:
            kind = "lights"
        elif wheel:
            kind = "wheel"
        else:
            kind = "textured"
        key = (uid, kind)
        if key not in mat_index:
            base = {
                "paint": paint_lin,
                "metallic": paint_lin,
                "trim": np.array([0.02, 0.02, 0.022]),
                "tyre": np.array([0.018, 0.018, 0.018]),
                "chrome": np.array([0.85, 0.86, 0.88]),
            }.get(kind, np.array([0.3, 0.3, 0.3]))
            mat_index[key] = len(materials)
            m = Material(kind, tex, np.asarray(base, np.float32), packed, car_paint if kind in PAINT_KINDS else None)
            if physical:
                _physical(m, uid, info, name, texture_of)
            materials.append(m)
        return mat_index[key]

    parts_pos, parts_nrm, parts_uv, parts_ao, parts_mat, parts_in, parts_lamp = [], [], [], [], [], [], []
    parts_wheel = []
    lamps: list[LampInfo] = []
    lamp_index: dict[int, int] = {}

    def add(pos, nrm, uvs, ao, m, inside=False, s9=None, wheel=False):
        if not len(pos):
            return
        parts_in.append(np.full(len(pos), inside))
        parts_wheel.append(np.full(len(pos), wheel))
        parts_pos.append(pos)
        parts_nrm.append(nrm if nrm is not None else _face_normals(pos))
        parts_uv.append(uvs)
        parts_ao.append(ao)
        parts_mat.append(np.full(len(pos), m, np.int32))
        lamp = np.full(len(pos), -1, np.int32)
        if s9 is not None and lamp_of:
            byte = s9[:, :, 2].max(axis=1)
            for b in np.unique(byte[byte > 0]):
                b = int(b)
                if b in lamp_of and b not in lamp_index:
                    lamp_index[b] = len(lamps)
                    lamps.append(lamp_of[b])
                if b in lamp_index:
                    lamp[byte == b] = lamp_index[b]
        parts_lamp.append(lamp)

    if entry == "interior":
        order = (1,) if 1 in index else ()
    elif entry == "full":
        order = (0, 1) if interior and 1 in index else (0,)
    else:
        order = (2, 1) if interior and 1 in index else (2,)
    for kind in order:
        _, mesh = vm.load(archive, index[kind], entries)
        cache, seen = {}, set()
        for p, part in enumerate(mesh.parts):
            lods = [lod for lod in part.lods if lod.mask == (mask or FULL_MASK)] if kind == 0 else part.lods
            subs = [mesh.submodels[n] for lod in lods for n in lod.submodels]
            wheel = kind in (0, 2) and any(
                s.material in TYRE_UIDS or mesh.vertex_buffers[s.vertex_buffer].stride == 20 for s in subs
            )
            if kind == 0 and p != 0 and not wheel:
                continue  # damage panel parts and chassis variants: part 0 again
            on = [hubs[i] for i in sorted(set(part_hubs[p]))] if wheel and p in part_hubs else hubs
            for sub in subs:
                key = (sub.vertex_buffer, sub.index_buffer, sub.first_index, sub.index_count, sub.base_vertex)
                if key in seen:
                    continue  # parts that list the same geometry (exterior parts 0 and 1)
                seen.add(key)
                if (
                    kind == 1
                    and entry != "interior"
                    and (
                        sub.material in GLASS_UIDS
                        or sub.material in INTERIOR_GLASS_UIDS
                        or mesh.vertex_buffers[sub.vertex_buffer].stride == 20
                    )
                ):
                    continue  # the exterior glass covers the cabin; needles are tiny
                if wheel and _fades_in(mesh, sub, cache):
                    continue  # the speed-blur disc: drawn only as the wheel spins (#221)
                pos, nrm, uvs, ao = _submodel_triangles(mesh, sub, cache)
                s9 = _submodel_s9(mesh, sub, cache) if physical else None
                m = material(sub.material, kind == 1, wheel)
                if wheel:
                    for hub, q in on:
                        r = _quat_matrix(q)
                        add(pos @ r.T + hub, None if nrm is None else nrm @ r.T, uvs, ao, m, wheel=True)
                else:
                    add(pos, nrm, uvs, ao, m, kind == 1, s9)
    return Scene(
        np.concatenate(parts_pos).astype(np.float32),
        np.concatenate(parts_nrm).astype(np.float32),
        np.concatenate(parts_uv).astype(np.float32),
        np.concatenate(parts_ao).astype(np.float32),
        np.concatenate(parts_mat),
        materials,
        np.concatenate(parts_in),
        np.concatenate(parts_lamp) if physical else None,
        lamps if physical else None,
        None,
        {
            "hubs": hubs,
            "part_hubs": part_hubs,
            "paint": car_paint,
            "entry": entry,
            "mask": mask,
            "wheel": np.concatenate(parts_wheel),  # (T,): on a wheel part (set on the hubs)
        },
    )


def _physical(m: Material, uid: bytes, info: dict, name: str, texture_of) -> None:
    """Material m as its DSF program draws it (dsf_shading), in place."""
    if m.kind in PAINT_KINDS:
        m.program = "paint"
    elif m.kind == "glass":
        m.program = "glass"
    elif m.kind in ("trim", "tyre", "chrome"):
        m.program = m.kind
        if m.kind == "chrome":
            m.texture, m.packed = texture_of(uid)
    elif "_lights" in name or "g_texDecalNormalMap" in info.get("params", ()):
        floats = info.get("floats", {})
        # The host's Lights material keeps specular 1; its gloss copies (dsf_materials
        # GlossMaterial) take 0.0625 at most.
        m.program = "lights" if floats.get("g_hSpecularIntensity", 1.0) >= 0.5 else "gloss"
        m.emissive = True
    else:
        m.program = "chassis"
    m.name, m.uid = info.get("path", ""), uid
    if m.program == "paint":
        # The garage tone's paint, through the same terms as every other material here.
        sh = SHADING[m.kind]
        m.shading = Shading(sh.diffuse, sh.f0, sh.reflect, sh.gloss, sh.highlight, sh.power)
    elif m.program != "glass":
        m.shading = dsf_shading(m.program, info.get("floats"))


def _srgb_to_linear(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


# --- rasteriser ----------------------------------------------------------------------

TILE = 16  # largest tile side, pixels
BACK_FACE_BIAS = 4e-4  # relative depth: about 2 mm at the camera's distance
INTERIOR_BIAS = (2e-3, 3e-2)  # behind glass, elsewhere: about 1.3 cm and 20 cm
INTERIOR_LENS_BIAS = 0.5  # behind lamp glass (kind "lens"): the interior loses to any exterior surface


def _fragments(sx, sy, sw, width: int, height: int, chunk: int = 1 << 18):
    """Every pixel centre (x + 0.5, y + 0.5) inside a screen triangle (T, 3):
    (pixel index y * width + x, 1 / z there, triangle index). Triangles are cut
    into tiles of 1 to TILE pixels a side, and all tiles of one size are tested
    at once."""
    x0 = np.clip(np.ceil(sx.min(axis=1) - 0.5), 0, width).astype(np.int64)
    x1 = np.clip(np.floor(sx.max(axis=1) - 0.5) + 1, 0, width).astype(np.int64)
    y0 = np.clip(np.ceil(sy.min(axis=1) - 0.5), 0, height).astype(np.int64)
    y1 = np.clip(np.floor(sy.max(axis=1) - 0.5) + 1, 0, height).astype(np.int64)
    area = (sx[:, 1] - sx[:, 0]) * (sy[:, 2] - sy[:, 0]) - (sy[:, 1] - sy[:, 0]) * (sx[:, 2] - sx[:, 0])
    drawn = np.flatnonzero((x0 < x1) & (y0 < y1) & (np.abs(area) > 1e-12))
    ks, ws, ts = [], [], []
    if not len(drawn):
        empty = np.zeros(0, np.int64)
        return empty, np.zeros(0, np.float32), empty
    sides = [1 << b for b in range(TILE.bit_length())]
    bw, bh = (x1 - x0)[drawn], (y1 - y0)[drawn]
    tw, th = np.ones_like(bw), np.ones_like(bh)
    for side in sides[1:]:
        tw[bw > side // 2] = side
        th[bh > side // 2] = side
    nx, ny = -(-bw // tw), -(-bh // th)
    for side_w in sides:
        for side_h in sides:
            sel = np.flatnonzero((tw == side_w) & (th == side_h))
            if not len(sel):
                continue
            count = nx[sel] * ny[sel]
            item = np.repeat(drawn[sel], count)
            local = np.arange(len(item), dtype=np.int64) - np.repeat(np.cumsum(count) - count, count)
            item_nx = np.repeat(nx[sel], count)
            item_x = x0[item] + (local % item_nx) * side_w
            item_y = y0[item] + (local // item_nx) * side_h
            step = max(1, chunk // (side_w * side_h))
            cols = np.arange(side_w, dtype=np.int64)[:, None]
            rows = np.arange(side_h, dtype=np.int64)[:, None]
            for start in range(0, len(item), step):
                t = item[start : start + step]
                x = cols + item_x[start : start + step]  # (side_w, n)
                y = rows + item_y[start : start + step]  # (side_h, n)
                xc = np.where(x < x1[t], x + 0.5, np.nan)[None]  # (1, side_w, n)
                yc = np.where(y < y1[t], y + 0.5, np.nan)[:, None]  # (side_h, 1, n)
                ax, ay, bx, by, cx, cy = sx[t, 0], sy[t, 0], sx[t, 1], sy[t, 1], sx[t, 2], sy[t, 2]
                a = area[t]
                l0 = ((bx - xc) * (cy - yc) - (by - yc) * (cx - xc)) / a
                l1 = ((cx - xc) * (ay - yc) - (cy - yc) * (ax - xc)) / a
                l2 = 1 - l0 - l1
                inside = (l0 >= 0) & (l1 >= 0) & (l2 >= 0)
                f = np.flatnonzero(inside)
                if not len(f):
                    continue
                r, rest = np.divmod(f, side_w * len(t))
                c, n = np.divmod(rest, len(t))
                tri = t[n]
                w = l0.ravel()[f] * sw[tri, 0] + l1.ravel()[f] * sw[tri, 1] + l2.ravel()[f] * sw[tri, 2]
                ks.append(y[r, n] * width + x[c, n])
                ws.append(w.astype(np.float32))
                ts.append(tri)
    if not ks:
        empty = np.zeros(0, np.int64)
        return empty, np.zeros(0, np.float32), empty
    return np.concatenate(ks), np.concatenate(ws), np.concatenate(ts)


def _nearest(k, w, t, size: int):
    """Per pixel the fragment with the largest 1 / z (the earliest triangle among
    equals): (depth (size,), triangle (size,), -1 where none)."""
    depth = np.full(size, -np.inf, np.float32)
    np.maximum.at(depth, k, w)
    win = w == depth[k]
    tri = np.full(size, np.iinfo(np.int64).max, np.int64)
    np.minimum.at(tri, k[win], t[win])
    tri[tri == np.iinfo(np.int64).max] = -1
    return depth, tri


# --- shading -------------------------------------------------------------------------


def _environment(d: np.ndarray) -> np.ndarray:
    """Radiance arriving from direction d (N, 3), world frame (+y up): a soft
    studio sky over a dark floor."""
    y = d[:, 1:2]
    sky = ENV_HORIZON + (ENV_ZENITH - ENV_HORIZON) * np.sqrt(np.clip(y, 0, 1))
    floor = ENV_GROUND + (ENV_FLOOR_FAR - ENV_GROUND) * np.clip(1 + y * 4, 0, 1)
    t = np.clip((y + 0.02) / 0.08, 0, 1)
    t = t * t * (3 - 2 * t)
    # A soft box above and beyond the car (as the camera sees it): the bright streak
    # bonnets and roofs show.
    box = np.clip((d @ SOFTBOX_DIR - SOFTBOX_EDGE[0]) / (SOFTBOX_EDGE[1] - SOFTBOX_EDGE[0]), 0, 1)[:, None]
    return floor * (1 - t) + sky * t + SOFTBOX * box * box * (3 - 2 * box)


def _sample(tex: np.ndarray, uv: np.ndarray) -> np.ndarray:
    h, w = tex.shape[:2]
    x = np.clip(((uv[:, 0] % 1) * w).astype(np.int64), 0, w - 1)
    y = np.clip(((uv[:, 1] % 1) * h).astype(np.int64), 0, h - 1)
    return tex[y, x]


class _View:
    def __init__(self, scene: Scene, camera: Camera, width: int, height: int, box=None):
        """box: the (lo, hi) corners to frame instead of the scene's own (render_compare
        frames our car and Forza's alike)."""
        if box is None:
            pts = scene.pos.reshape(-1, 3)
            lo, hi = pts.min(axis=0), pts.max(axis=0)
        else:
            lo, hi = (np.asarray(c, np.float64) for c in box)
        self.centre = (lo + hi) / 2
        length = float(max(hi - lo))
        self.width, self.height = width, height
        self.ground = float(lo[1])
        self.lo, self.hi = lo, hi
        if camera.eye is not None:
            self.eye = np.asarray(camera.eye, np.float64)
            self.fwd = np.asarray(camera.look, np.float64) / np.linalg.norm(camera.look)
            self.eye_dir = -self.fwd
            self.right = np.cross(self.fwd, [0.0, 1.0, 0.0])
            self.right /= np.linalg.norm(self.right)
            self.up = np.cross(self.right, self.fwd)
            self.focal = width / 2 / math.tan(math.radians(camera.fov) / 2)
            return
        ya, pa = math.radians(camera.yaw), math.radians(camera.pitch)
        # DSF: +z forward, +y up, +x the car's left: the camera on the front right.
        self.eye_dir = np.array([-math.sin(ya) * math.cos(pa), math.sin(pa), math.cos(ya) * math.cos(pa)])
        dist = camera.distance * length
        self.eye = self.centre + self.eye_dir * dist
        self.fwd = -self.eye_dir
        self.right = np.cross(self.fwd, [0.0, 1.0, 0.0])
        self.right /= np.linalg.norm(self.right)
        self.up = np.cross(self.right, self.fwd)
        # Field of view: the car's bounding sphere fills the frame's width.
        radius = float(np.linalg.norm(hi - lo)) / 2
        self.focal = width / 2 / math.tan(math.asin(min(0.99, radius / dist)))

    def project(self, p: np.ndarray):
        d = p - self.eye
        z = d @ self.fwd
        x = self.width / 2 + self.focal * (d @ self.right) / z
        y = self.height / 2 - self.focal * (d @ self.up) / z
        return x, y, z

    def rays(self, k: np.ndarray) -> np.ndarray:
        """Unit view rays (eye to pixel centre) for pixel indices k."""
        px = (k % self.width + 0.5 - self.width / 2) / self.focal
        py = -(k // self.width + 0.5 - self.height / 2) / self.focal
        d = self.fwd[None] + px[:, None] * self.right[None] + py[:, None] * self.up[None]
        return d / np.linalg.norm(d, axis=1, keepdims=True)


def _light(view: _View) -> np.ndarray:
    """The key light, turned with the camera so it always comes from its upper left."""
    lx, ly, lz = KEY_LIGHT
    d = lx * view.right + ly * np.array([0.0, 1.0, 0.0]) + lz * view.eye_dir
    return d / np.linalg.norm(d)


def _barycentrics(view: _View, sx, sy, sw, k: np.ndarray, tri: np.ndarray) -> np.ndarray:
    """Perspective-correct barycentrics (n, 3) of pixels k on triangles tri."""
    px, py = (k % view.width) + 0.5, (k // view.width) + 0.5
    ax, ay, bx, by, cx, cy = sx[tri, 0], sy[tri, 0], sx[tri, 1], sy[tri, 1], sx[tri, 2], sy[tri, 2]
    a = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    l0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / a
    l1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / a
    lam = np.stack((l0, l1, 1 - l0 - l1), axis=1) * sw[tri]
    lam /= np.maximum(lam.sum(axis=1, keepdims=True), 1e-12)
    return lam


def _surface(scene: Scene, view: _View, sx, sy, sw, k: np.ndarray, tri: np.ndarray):
    """Interpolated attributes at pixels k on triangles tri: (normal, uv, ao, view ray)."""
    lam = _barycentrics(view, sx, sy, sw, k, tri)
    n = np.einsum("nc,nci->ni", lam, scene.nrm[tri])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
    uv = np.einsum("nc,nci->ni", lam, scene.uv[tri])
    ao = np.einsum("nc,nc->n", lam, scene.ao[tri])
    ray = view.rays(k)
    # Double-sided: the normal faces the camera.
    flip = np.einsum("ni,ni->n", n, ray) > 0
    n[flip] *= -1
    return n, uv, ao, ray


@dataclass(frozen=True)
class Lighting:
    """The light a frame is shaded in: scales of the environment (sky and reflections),
    the sky ambient and the key light, and the lamps that are on (a Lua light mask:
    1 Headlights, 2 Brakes, 4 DaytimeRunning; research/lights.md, "The light table")."""

    name: str = "day"
    env: float = 1.0
    ambient: float = 1.0
    key: float = 1.0
    lamps: int = 0


DAY = Lighting()
DAY_LIT = Lighting("day_lit", lamps=4)  # the running lights, as DSF lights them by day
DARK = Lighting("dark", 0.05, 0.05, 0.0, 5)  # the dark mood: running lights and head lamps
LIGHTINGS = {lt.name: lt for lt in (DAY, DAY_LIT, DARK)}
EMISSION = 2.0  # linear light of a lamp texel of alpha 1 at brightness 1 (exposure follows)

# DSF's lamp colours by the light table's colour byte (b2) and brightness per type byte
# (b3; 0xF3FB68, research/lights.md "How a lamp lights"), over a 0x01 head lamp's 2.
LAMP_COLOURS = {
    8: (1.0, 0.95, 0.85),
    9: (0.85, 0.92, 1.0),
    4: (1.0, 0.45, 0.05),
    6: (1.0, 0.06, 0.03),
}
# (one assignment: statements other than definitions would change the module's
# top-level code unit, which every car's build fingerprint holds; build/fingerprint.py)
TYPE_BRIGHTNESS = {
    1: 2,
    2: 1,
    3: 2,
    **dict.fromkeys(range(4, 8), 0.5),
    8: 1,
    **dict.fromkeys(range(9, 18), 10),
    18: 3,
    19: 10,
    **dict.fromkeys(range(20, 29), 1),
    29: 10,
    30: 0.2,
}
COMBINED_TYPES = {0x13, *range(0x18, 0x1E)}


def lamp_level(type_byte: int, on: int) -> float:
    """How bright a DSF lamp of this type shows with the light mask `on` (0..1 target
    times the type's brightness, over a 0x01 head lamp's): the combined types light at
    full when braking, with the running lights alone at 0.2 (0x18..0x1C) or 1 (0x13, 0x1D)."""
    from dsfforza.convert import lights

    if not 0 < type_byte < len(lights.LIGHT_TYPE_MASK):
        return 0.0
    mask = lights.LIGHT_TYPE_MASK[type_byte]
    if not mask & on:
        return 0.0
    level = 1.0
    if type_byte in COMBINED_TYPES and not (mask & on & 3):
        level = 0.2 if 0x18 <= type_byte <= 0x1C else 1.0
    return level * TYPE_BRIGHTNESS.get(type_byte, 1) / TYPE_BRIGHTNESS[1]


def dsf_lamp_rgb(lamps: list[LampInfo] | None, lighting: Lighting) -> np.ndarray | None:
    """(L, 3) linear emission colour of each lamp in this light (None without lamps)."""
    if not lamps:
        return None
    return np.array(
        [np.array(LAMP_COLOURS.get(lp.colour, LAMP_COLOURS[8])) * lamp_level(lp.type, lighting.lamps) for lp in lamps],
        np.float32,
    ).reshape(-1, 3)


@dataclass
class Frame:
    """One view rasterised: per pixel the nearest opaque triangle (tri, -1 for none) and
    its 1 / z (depth), and up to `layers` see-through layers in front of it (pixels,
    triangles), nearest first. tri and the layers are the ID buffer."""

    view: _View
    sx: np.ndarray
    sy: np.ndarray
    sw: np.ndarray
    depth: np.ndarray
    tri: np.ndarray
    layers: list


def _alpha_cut(scene: Scene, view: _View, sx, sy, sw, k, w, t):
    """Drop the fragments of alpha-tested materials on texels with alpha under 0.5."""
    tested = np.array([m.alpha_test and m.texture is not None for m in scene.materials])
    if not tested.any():
        return k, w, t
    sel = np.flatnonzero(tested[scene.mat[t]])
    if not len(sel):
        return k, w, t
    lam = _barycentrics(view, sx, sy, sw, k[sel], t[sel])
    uv = np.einsum("nc,nci->ni", lam, scene.uv[t[sel]])
    drop = np.zeros(len(sel), bool)
    mats = scene.mat[t[sel]]
    for m in np.unique(mats):
        on = mats == m
        drop[on] = _sample(scene.materials[m].texture, uv[on])[:, 3] < 0.5
    keep = np.ones(len(k), bool)
    keep[sel[drop]] = False
    return k[keep], w[keep], t[keep]


def rasterize(scene: Scene, view: _View, layers: int = 2) -> Frame:
    """The frame's ID buffer: nearest opaque triangle per pixel and the see-through
    layers in front of it."""
    width, height = view.width, view.height
    sx, sy, sz = view.project(scene.pos)
    ok = sz.min(axis=1) > 1e-3
    sw = np.where(ok[:, None], 1 / np.maximum(sz, 1e-3), 0)
    size = width * height
    glass = scene.transparent
    opaque_t = np.flatnonzero(ok & ~glass)
    glass_t = np.flatnonzero(ok & glass)
    k, w, t = _fragments(sx[opaque_t], sy[opaque_t], sw[opaque_t], width, height)
    gk, gw, gt = _fragments(sx[glass_t], sy[glass_t], sw[glass_t], width, height)
    gt = glass_t[gt]
    if len(k):
        k, w, t = _alpha_cut(scene, view, sx, sy, sw, k, w, opaque_t[t])
        t = np.searchsorted(opaque_t, t)
    # Surfaces are drawn from both sides, but a back face loses a tie: a panel's
    # underside (paint, on the engine-bay picture: damage.md) lies on the panel itself
    # and the game culls it.
    centre = scene.pos.mean(axis=1)
    back = np.einsum("ni,ni->n", scene.nrm.mean(axis=1), centre - view.eye) > 0
    w = np.where(back[opaque_t[t]], w * (1 - BACK_FACE_BIAS), w)
    # The interior entry holds the cockpit's own copy of the body shell, close to the
    # exterior and in places outside it (a dark bonnet patch on the S2000, RX-7, Cuda):
    # interior surfaces lose to the exterior unless clearly nearer, by a little behind
    # glass (the cabin), by a lot elsewhere (an open car's seats are). The cabin is never
    # seen through a lamp lens (render_compare's Forza reference draws them see-through):
    # behind one it loses to any exterior surface. With the window bias there, the cockpit's
    # copy of the front and rear panels won over the lamp bowls and reflectors recessed 1 to
    # 10 cm behind it, and their pixels came out empty (#313).
    if scene.inside is not None and scene.inside.any() and not scene.inside.all():
        kinds = np.array([m.kind for m in scene.materials])[scene.mat[gt]]
        glass_w = np.full(size, -np.inf, np.float32)
        np.maximum.at(glass_w, gk[kinds == "glass"], gw[kinds == "glass"])
        lens_w = np.full(size, -np.inf, np.float32)
        np.maximum.at(lens_w, gk[kinds == "lens"], gw[kinds == "lens"])
        bias = np.where(glass_w[k] > w, INTERIOR_BIAS[0], INTERIOR_BIAS[1])
        bias = np.where(lens_w[k] > w, INTERIOR_LENS_BIAS, bias)
        w = np.where(scene.inside[opaque_t[t]], w * (1 - bias), w)
    depth, tri = _nearest(k, w, opaque_t[t], size)
    out = []
    # See-through surfaces: the nearest layer in front of the opaque surface, then
    # the ones behind it.
    if len(gk):
        front = gw > depth[gk]
        gk, gw, gt = gk[front], gw[front], gt[front]
        for _ in range(layers):
            if not len(gk):
                break
            gdepth, gtri = _nearest(gk, gw, gt, size)
            on = np.flatnonzero(gtri >= 0)
            out.append((on, gtri[on]))
            behind = gw < gdepth[gk] * (1 - 1e-4)
            gk, gw, gt = gk[behind], gw[behind], gt[behind]
    return Frame(view, sx, sy, sw, depth, tri, out)


def _base(mat: Material, t: np.ndarray | None, colour: np.ndarray, n: int) -> np.ndarray:
    """Linear base colour of n points of a material (t: their texels, or None)."""
    if mat.kind in PAINT_KINDS:
        if t is not None:
            return _srgb_to_linear(t[:, 0:1]) * (1 + (colour - 1) * t[:, 2:3])
        return colour
    if mat.packed and t is not None and (mat.kind != "chrome" or mat.shading is not None):
        return np.repeat(_srgb_to_linear(t[:, 0:1]), 3, axis=1)  # compare renders: chrome's texel too
    if t is not None and mat.kind not in ("chrome",):
        return _srgb_to_linear(t[:, :3])
    return np.broadcast_to(mat.colour, (n, 3))


def _shade(
    scene: Scene, view: _View, light, n, uv, ao, ray, mats, lighting: Lighting = DAY, lamp=None, lamp_rgb=None
) -> tuple[np.ndarray, np.ndarray]:
    """Linear colour of opaque surface points, and their Fresnel term. lamp (N,): each
    point's lamp index (-1 none) into lamp_rgb (L, 3) for the emissive materials."""
    out = np.zeros((len(n), 3), np.float32)
    fres = np.zeros(len(n), np.float32)
    # The baked term is Forza's AO times 0.25 + 0.55 (1 + n_y) / 2 (vehicle-model.md): undo the second factor.
    occlusion = np.clip(ao / (0.25 + 0.55 * (1 + n[:, 1]) / 2), 0.15, 1.0)
    for m in np.unique(mats):
        sel = np.flatnonzero(mats == m)
        mat = scene.materials[m]
        if mat.shading is not None:
            out[sel], fres[sel] = _shade_physical(mat, view, light, n[sel], uv[sel], occlusion[sel], ray[sel], lighting)
            if mat.emissive and lamp_rgb is not None and lamp is not None and mat.texture is not None:
                on = lamp[sel] >= 0
                if on.any():
                    a = _sample(mat.texture, uv[sel[on]])[:, 3:4]
                    out[sel[on]] += a * lamp_rgb[lamp[sel[on]]] * EMISSION
            continue
        sh = SHADING[mat.kind]
        nn, rr = n[sel], ray[sel]
        cos_v = np.clip(-np.einsum("ni,ni->n", nn, rr), 0, 1)
        if mat.kind in PAINT_KINDS:
            # The palette entry's colour 2 mixed toward colour 3 by the view angle, as the
            # paint shader does; tinted = tex.r * lerp(1, paint, tex.b) (lights.md): the
            # shared atlas is channel-packed, red the grey level, blue the paint mask.
            colour = mat.paint.at(cos_v) if mat.paint is not None else np.broadcast_to(mat.colour, (len(sel), 3))
        else:
            colour = mat.colour
        base = _base(mat, _sample(mat.texture, uv[sel]) if mat.texture is not None else None, colour, len(sel))
        f = sh.f0 + (1 - sh.f0) * (1 - cos_v) ** 5
        refl = rr - 2 * np.einsum("ni,ni->n", rr, nn)[:, None] * nn
        blur = refl * sh.gloss + nn * (1 - sh.gloss)
        blur /= np.maximum(np.linalg.norm(blur, axis=1, keepdims=True), 1e-9)
        env = _environment(blur) * lighting.env
        occ = occlusion[sel, None]
        ambient = (GROUND_AMBIENT + (SKY_AMBIENT - GROUND_AMBIENT) * (1 + nn[:, 1:2]) / 2) * occ * lighting.ambient
        lambert = np.clip(nn @ light, 0, None)[:, None] * KEY_POWER * lighting.key
        half = light[None] - rr
        half /= np.linalg.norm(half, axis=1, keepdims=True)
        spec = sh.highlight * np.clip(np.einsum("ni,ni->n", nn, half), 0, None) ** sh.power * lighting.key
        diffuse = base * (ambient + lambert * np.clip(occ * 1.6, 0, 1)) * sh.diffuse
        r = (f * sh.reflect)[:, None]
        if mat.kind == "chrome":
            colour = diffuse + env * base * r * occ + spec[:, None] * occ
        else:
            colour = diffuse * (1 - r) + env * r * np.clip(occ * 1.6, 0, 1) + spec[:, None] * occ
        out[sel] = colour
        fres[sel] = f
    return out, fres


def _channel(t: np.ndarray | None, mask: str | None, n: int) -> np.ndarray:
    if mask is None or t is None:
        return np.ones(n, np.float32)
    return t[:, {"r": 0, "g": 1, "b": 2, "a": 3}[mask]]


SUN_ELEVATION = 25.0  # degrees: compare renders' sun, beyond the car as the camera sees it
SUN_POWER = 1.0  # its specular against the key light's (no diffuse: the tone stays the garage's)


def _sun(view: _View) -> np.ndarray:
    """Towards the sun: from beyond the car, SUN_ELEVATION above the horizon."""
    away = -view.eye_dir * np.array([1.0, 0.0, 1.0])
    away /= max(np.linalg.norm(away), 1e-9)
    e = math.radians(SUN_ELEVATION)
    return away * math.cos(e) + np.array([0.0, math.sin(e), 0.0])


def _shade_physical(mat: Material, view: _View, light, nn, uv, occlusion, rr, lighting: Lighting):
    """A material with its own Shading (dsf_shading, forza_render): light x base plus
    Phong specular and a Fresnel reflection, each masked by a texel channel."""
    sh = mat.shading
    cos_v = np.clip(-np.einsum("ni,ni->n", nn, rr), 0, 1)
    t = _sample(mat.texture, uv) if mat.texture is not None else None
    colour = mat.paint.at(cos_v) if mat.kind in PAINT_KINDS and mat.paint is not None else mat.colour
    base = _base(mat, t, colour, len(nn)) * mat.scale
    if mat.paint_blend and t is not None:
        paint = mat.paint.at(cos_v) if mat.paint is not None else mat.colour
        a = t[:, 3:4]
        base = base * a + paint * (1 - a)
    f = sh.f0 + (1 - sh.f0) * (1 - cos_v) ** sh.fresnel_power
    refl = rr - 2 * np.einsum("ni,ni->n", rr, nn)[:, None] * nn
    blur = refl * sh.gloss + nn * (1 - sh.gloss)
    blur /= np.maximum(np.linalg.norm(blur, axis=1, keepdims=True), 1e-9)
    env = _environment(blur) * lighting.env
    occ = occlusion[:, None]
    ambient = (GROUND_AMBIENT + (SKY_AMBIENT - GROUND_AMBIENT) * (1 + nn[:, 1:2]) / 2) * occ * lighting.ambient
    lambert = np.clip(nn @ light, 0, None)[:, None] * KEY_POWER * lighting.key
    if sh.phong:
        lobe = np.clip(refl @ light, 0, None)
    else:
        half = light[None] - rr
        half /= np.linalg.norm(half, axis=1, keepdims=True)
        lobe = np.clip(np.einsum("ni,ni->n", nn, half), 0, None)
    spec = sh.highlight * lobe**sh.power * lighting.key
    if sh.phong:  # compare renders: the sun ahead of the camera too (glints on flat panels, #176)
        spec = spec + sh.highlight * np.clip(refl @ _sun(view), 0, None) ** sh.power * SUN_POWER * lighting.key
    spec = spec * _channel(t, sh.spec_mask, len(nn))
    r = f * sh.reflect * _channel(t, sh.mask, len(nn))
    out = (
        base * (ambient + lambert * np.clip(occ * 1.6, 0, 1)) * sh.diffuse
        + env * r[:, None] * np.clip(occ * 1.6, 0, 1)
        + spec[:, None] * occ
    )
    return out, f


def _shade_glass(view: _View, light, n, ray, lighting: Lighting = DAY) -> tuple[np.ndarray, np.ndarray]:
    """(reflected light added on top, fraction of the light behind let through)."""
    sh = SHADING["glass"]
    cos_v = np.clip(-np.einsum("ni,ni->n", n, ray), 0, 1)
    f = sh.f0 + (1 - sh.f0) * (1 - cos_v) ** 5
    refl = ray - 2 * np.einsum("ni,ni->n", ray, n)[:, None] * n
    half = light[None] - ray
    half /= np.linalg.norm(half, axis=1, keepdims=True)
    spec = sh.highlight * np.clip(np.einsum("ni,ni->n", n, half), 0, None) ** sh.power * lighting.key
    add = _environment(refl) * lighting.env * f[:, None] * sh.reflect + spec[:, None] + GLASS_HAZE * lighting.ambient
    through = (1 - f)[:, None] * GLASS_TINT
    return add, through


def _shade_lens(mat: Material, view: _View, light, n, uv, ray, occlusion, lighting: Lighting, lamp, lamp_rgb):
    """See-through lamp glass (Material.lens): (light added on top, light behind let
    through (N, 3)). The texel's opaque share is lit like a surface and tints what is
    behind; an emissive lens glows by its texel alpha."""
    lens = mat.lens or Lens()
    sh = mat.shading or SHADING["glass"]
    if mat.texture is not None:
        t = _sample(mat.texture, uv)
        o = np.clip(np.maximum(t[:, 3] * lens.alpha, lens.floor), 0, lens.cap)[:, None]
    else:  # untextured glass: the opacity floor (GlassColor.w), its colour the material's
        t = np.concatenate([np.broadcast_to(mat.colour, (len(n), 3)), np.zeros((len(n), 1))], axis=1)
        t = t.astype(np.float32)
        o = np.full((len(n), 1), min(lens.floor, lens.cap), np.float32)
    texel = _srgb_to_linear(t[:, :3])
    lin = texel * mat.scale
    cos_v = np.clip(-np.einsum("ni,ni->n", n, ray), 0, 1)
    f = sh.f0 + (1 - sh.f0) * (1 - cos_v) ** sh.fresnel_power
    refl = ray - 2 * np.einsum("ni,ni->n", ray, n)[:, None] * n
    lobe = np.clip(refl @ light, 0, None)
    spec = sh.highlight * lobe**sh.power * lighting.key
    occ = occlusion[:, None]
    ambient = (GROUND_AMBIENT + (SKY_AMBIENT - GROUND_AMBIENT) * (1 + n[:, 1:2]) / 2) * occ * lighting.ambient
    lambert = np.clip(n @ light, 0, None)[:, None] * KEY_POWER * lighting.key
    add = o * lin * (ambient + lambert) * sh.diffuse + _environment(refl) * lighting.env * (f * sh.reflect)[:, None]
    add += spec[:, None]
    if mat.emissive and lamp_rgb is not None and lamp is not None:
        on = lamp >= 0
        add[on] += t[on, 3:4] * lamp_rgb[lamp[on]] * EMISSION
    through = ((1 - o) + o * np.clip(texel * lens.tint, 0, 1)) * (1 - f)[:, None]
    return add, np.clip(through, 0, 1)


def _shadow(view: _View, k: np.ndarray) -> np.ndarray:
    """Soft contact shadow alpha where the view ray meets the floor under the car."""
    ray = view.rays(k)
    t = (view.ground - view.eye[1]) / np.minimum(ray[:, 1], -1e-6)
    hit = view.eye[None] + ray * t[:, None]
    half = (view.hi - view.lo) / 2
    cx, cz = view.centre[0], view.centre[2]
    dx = np.abs(hit[:, 0] - cx) - half[0] * 0.92
    dz = np.abs(hit[:, 2] - cz) - half[2] * 0.95
    outside = np.hypot(np.clip(dx, 0, None), np.clip(dz, 0, None)) + np.minimum(np.maximum(dx, dz), 0)
    soft = 0.28  # m
    a = np.clip(0.5 - outside / soft, 0, 1)
    a = a * a * (3 - 2 * a)
    return np.where(t > 0, a * SHADOW_ALPHA, 0.0)


def coverage(scene: Scene, camera: Camera, width: int, height: int) -> np.ndarray:
    """(height, width) bool: pixels the car covers (for the camera fit)."""
    view = _View(scene, camera, width, height)
    sx, sy, sz = view.project(scene.pos)
    keep = np.flatnonzero(sz.min(axis=1) > 1e-3)
    k, _, _ = _fragments(sx[keep], sy[keep], 1 / sz[keep], width, height)
    mask = np.zeros(width * height, bool)
    mask[k] = True
    return mask.reshape(height, width)


FIT_GRID = (range(24, 53, 4), range(0, 29, 4), (0.9, 1.1, 1.3, 1.6, 2.0, 2.6, 3.5, 5.0))


def fit_camera(scene: Scene, score, size: tuple[int, int] = (360, 240)) -> tuple[float, Camera]:
    """The camera whose coverage mask scores best: a coarse grid over yaw, pitch
    and distance (FIT_GRID), then 1-degree / 10% steps around the best."""
    best = (-math.inf, VANILLA_CAMERA)
    for yaw in FIT_GRID[0]:
        for pitch in FIT_GRID[1]:
            for dist in FIT_GRID[2]:
                cam = Camera(float(yaw), float(pitch), dist)
                best = max(best, (score(coverage(scene, cam, *size)), cam), key=lambda b: b[0])
    c = best[1]
    for yaw in np.arange(c.yaw - 3, c.yaw + 3.5, 1.0):
        for pitch in np.arange(max(0.0, c.pitch - 3), c.pitch + 3.5, 1.0):
            for f in (0.82, 0.9, 1.0, 1.1, 1.22):
                cam = Camera(float(yaw), float(pitch), round(c.distance * f, 3))
                best = max(best, (score(coverage(scene, cam, *size)), cam), key=lambda b: b[0])
    return best


def shade(
    scene: Scene, frame: Frame, lighting: Lighting = DAY, lamp_rgb: np.ndarray | None = None, shadow: bool = True
) -> tuple[np.ndarray, np.ndarray]:
    """Linear colour (size, 3) and straight alpha (size,) of a rasterised frame.
    lamp_rgb (L, 3): each of scene.lamps' emission in this light (dsf_lamp_rgb)."""
    view = frame.view
    light = _light(view)
    sx, sy, sw = frame.sx, frame.sy, frame.sw
    size = view.width * view.height
    tri = frame.tri
    colour = np.zeros((size, 3), np.float32)
    alpha = np.zeros(size, np.float32)
    hit = np.flatnonzero(tri >= 0)
    if len(hit):
        n, uv, ao, ray = _surface(scene, view, sx, sy, sw, hit, tri[hit])
        lamp = scene.lamp[tri[hit]] if scene.lamp is not None else None
        c, _ = _shade(scene, view, light, n, uv, ao, ray, scene.mat[tri[hit]], lighting, lamp, lamp_rgb)
        colour[hit] = c
        alpha[hit] = 1
    # Floor shadow under everything else.
    if shadow:
        pix = np.arange(size)
        shadow_a = _shadow(view, pix)
        empty = alpha == 0
        alpha[empty] = shadow_a[empty]
    # See-through layers, far first.
    for on, gtri in reversed(frame.layers):
        n, uv, ao, ray = _surface(scene, view, sx, sy, sw, on, gtri)
        add = np.zeros((len(on), 3), np.float32)
        through = np.zeros((len(on), 3), np.float32)
        mats = scene.mat[gtri]
        for m in np.unique(mats):
            sel = np.flatnonzero(mats == m)
            mat = scene.materials[m]
            if mat.kind == "lens":
                occ = np.clip(ao[sel] / (0.25 + 0.55 * (1 + n[sel, 1]) / 2), 0.15, 1.0)
                lamp = scene.lamp[gtri[sel]] if scene.lamp is not None else None
                add[sel], through[sel] = _shade_lens(
                    mat, view, light, n[sel], uv[sel], ray[sel], occ, lighting, lamp, lamp_rgb
                )
            else:
                add[sel], through[sel] = _shade_glass(view, light, n[sel], ray[sel], lighting)
        # Premultiplied: what is behind (or nothing) dimmed by the glass, plus its reflection.
        a = alpha[on]
        new_a = 1 - (1 - a) * through.mean(axis=1)
        premul = colour[on] * a[:, None] * through + add
        colour[on] = premul / np.maximum(new_a[:, None], 1e-6)
        alpha[on] = new_a
    return colour, alpha


def to_rgba(colour: np.ndarray, alpha: np.ndarray, width: int, height: int) -> np.ndarray:
    """(height, width, 4) uint8 straight-alpha RGBA of shade()'s linear colour."""
    rgb = _linear_to_srgb(colour * EXPOSURE)
    out = np.zeros((width * height, 4), np.uint8)
    out[:, :3] = np.clip(rgb * 255 + 0.5, 0, 255).astype(np.uint8)
    out[:, 3] = np.clip(alpha * 255 + 0.5, 0, 255).astype(np.uint8)
    return out.reshape(height, width, 4)


def render(scene: Scene, camera: Camera = VANILLA_CAMERA, width: int = 900, height: int = 480) -> np.ndarray:
    """(height, width, 4) uint8 straight-alpha RGBA: the car, its glass and its
    floor shadow on a transparent background."""
    frame = rasterize(scene, _View(scene, camera, width, height))
    colour, alpha = shade(scene, frame)
    return to_rgba(colour, alpha, width, height)
