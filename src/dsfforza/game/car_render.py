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

The camera matches the stock garage pictures (`STOCK_CAMERA`; measured with
`dsff gui_assets camera`, research/gui-assets.md "Camera").
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


STOCK_CAMERA = Camera(yaw=36.0, pitch=13.0, distance=1.2)  # median of 19 stock pictures (gui-assets.md, "Camera")


@dataclass
class Shading:
    """How a material class is lit (linear colour space)."""

    diffuse: float  # scale of the base colour under the lights
    f0: float  # Fresnel reflectance at normal incidence
    reflect: float  # environment reflection strength (times Fresnel)
    gloss: float  # 0 = the environment blurred to the normal, 1 = a mirror
    highlight: float  # key-light specular strength
    power: float  # key-light specular exponent


SHADING = {
    "paint": Shading(1.0, 0.05, 1.0, 1.0, 1.5, 150.0),
    "metallic": Shading(1.0, 0.08, 1.0, 1.0, 2.0, 80.0),
    "trim": Shading(1.0, 0.03, 0.35, 0.4, 0.15, 20.0),
    "tyre": Shading(1.0, 0.02, 0.1, 0.1, 0.05, 8.0),
    "chrome": Shading(0.15, 0.75, 1.0, 1.0, 2.0, 200.0),
    "lights": Shading(1.0, 0.06, 1.0, 1.0, 2.0, 200.0),
    "wheel": Shading(0.9, 0.2, 0.8, 0.7, 0.8, 60.0),
    "textured": Shading(1.0, 0.04, 0.6, 0.8, 0.5, 60.0),
    "interior": Shading(0.85, 0.02, 0.15, 0.3, 0.1, 10.0),
    "glass": Shading(0.0, 0.06, 1.0, 1.0, 2.5, 300.0),
}
GLASS_TINT = np.array([0.55, 0.6, 0.64])  # transmitted light through one glass layer (linear)
GLASS_HAZE = np.array([0.012, 0.014, 0.018])  # glass's own colour seen on top

# Studio environment, linear RGB.
ENV_ZENITH = np.array([1.0, 1.0, 1.0])
ENV_HORIZON = np.array([0.16, 0.17, 0.19])
ENV_GROUND = np.array([0.015, 0.015, 0.016])
ENV_FLOOR_FAR = np.array([0.06, 0.06, 0.065])
SOFTBOX_DIR = np.array([0.45, 0.75, -0.5]) / np.linalg.norm([0.45, 0.75, -0.5])
SOFTBOX_EDGE = (0.86, 0.95)  # cosines: dark outside the first, full inside the second
SOFTBOX = 3.0
KEY_LIGHT = (-0.5, 0.55, 0.55)  # towards the light, car frame before the camera turn (see _light)
KEY_POWER = 0.8
SKY_AMBIENT, GROUND_AMBIENT = 0.45, 0.03
EXPOSURE = 0.85
SHADOW_ALPHA = 0.75


# --- the scene -----------------------------------------------------------------------


@dataclass
class Material:
    kind: str  # a SHADING key
    texture: np.ndarray | None  # (h, w, 4) float32 0..1, sRGB as stored
    colour: np.ndarray  # linear RGB, the base without a texture (paint: the paint colour)
    packed: bool = False  # a shared SSIC texture: channel-packed, red is the grey level


@dataclass
class Scene:
    """Triangles (T): positions (T, 3, 3), normals (T, 3, 3), UV0 (T, 3, 2),
    sky / AO term (T, 3), material index (T,), glass flag (T,)."""

    pos: np.ndarray
    nrm: np.ndarray
    uv: np.ndarray
    ao: np.ndarray
    mat: np.ndarray
    materials: list[Material]

    @property
    def glass(self) -> np.ndarray:
        kinds = np.array([m.kind == "glass" for m in self.materials])
        return kinds[self.mat]


_SHARED: dict = {}


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
        _SHARED.clear()
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


def _face_normals(pos: np.ndarray) -> np.ndarray:
    n = np.cross(pos[:, 1] - pos[:, 0], pos[:, 2] - pos[:, 0])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
    return np.repeat(n[:, None], 3, axis=1).astype(np.float32)


def paint_colour(stream, full) -> tuple[tuple[int, int, int], bool]:
    """The palette's most colourful entry as sRGB (stock pictures rarely show
    black or grey), and whether it is metallic."""
    from dsfforza.convert import paint

    spss = paint.palette_set(stream, full)
    if spss is None or not spss.children:
        return (150, 20, 20), False
    options = []
    for e in spss.children:
        params = paint.spsp_params(stream, e)
        colour = tuple(int(255 * paint.linear_to_srgb(x)) for x in paint.apparent(params))
        metallic = "g_vec4TintColour1_Alpha" in params and paint.is_metallic(params)
        options.append((colour, metallic))
    return max(options, key=lambda o: (max(o[0]) - min(o[0]), -abs(sum(o[0]) / 3 - 128)))


def load_scene(archive: Path, spool_id: int, entries=None, interior: bool = True) -> Scene:
    """The clean exterior (kind 2) with its wheel part on every UPVH hub, and the
    interior (kind 1) without its glass and gauge needles."""

    archive = Path(archive)
    with archive.open("rb") as stream:
        return _load_scene(archive, stream, spool_id, entries, interior)


def _load_scene(archive: Path, stream, spool_id: int, entries, interior: bool) -> Scene:
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import upvh as uv
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.game.gui_assets import _root_entries

    if True:
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

        colour, metallic = paint_colour(stream, kinds[0])
        h = uv.parse(payload(stream, child(child(kinds[0], "NONR"), "UPVH")))
        hubs = []
        for i in range(h.counts["wheels"]):
            o = h.offsets["wheels"] + i * 0x20
            hubs.append(
                (np.array(struct.unpack_from("<3f", h.data, o), np.float32), struct.unpack_from("<4f", h.data, o + 16))
            )
        paint_lin = np.array([_srgb_to_linear(c / 255) for c in colour], np.float32)

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
                materials.append(Material(kind, tex, np.asarray(base, np.float32), packed))
            return mat_index[key]

    parts_pos, parts_nrm, parts_uv, parts_ao, parts_mat = [], [], [], [], []

    def add(pos, nrm, uvs, ao, m):
        if not len(pos):
            return
        parts_pos.append(pos)
        parts_nrm.append(nrm if nrm is not None else _face_normals(pos))
        parts_uv.append(uvs)
        parts_ao.append(ao)
        parts_mat.append(np.full(len(pos), m, np.int32))

    for kind in (2, 1) if interior and 1 in index else (2,):
        _, mesh = vm.load(archive, index[kind], entries)
        cache, seen = {}, set()
        for part in mesh.parts:
            subs = [mesh.submodels[n] for lod in part.lods for n in lod.submodels]
            wheel = kind == 2 and any(
                s.material in TYRE_UIDS or mesh.vertex_buffers[s.vertex_buffer].stride == 20 for s in subs
            )
            for sub in subs:
                key = (sub.vertex_buffer, sub.index_buffer, sub.first_index, sub.index_count, sub.base_vertex)
                if key in seen:
                    continue  # parts that list the same geometry (exterior parts 0 and 1)
                seen.add(key)
                if kind == 1 and (
                    sub.material in GLASS_UIDS
                    or sub.material in INTERIOR_GLASS_UIDS
                    or mesh.vertex_buffers[sub.vertex_buffer].stride == 20
                ):
                    continue  # the exterior glass covers the cabin; needles are tiny
                pos, nrm, uvs, ao = _submodel_triangles(mesh, sub, cache)
                m = material(sub.material, kind == 1, wheel)
                if wheel:
                    for hub, q in hubs:
                        r = _quat_matrix(q)
                        add(pos @ r.T + hub, None if nrm is None else nrm @ r.T, uvs, ao, m)
                else:
                    add(pos, nrm, uvs, ao, m)
    return Scene(
        np.concatenate(parts_pos).astype(np.float32),
        np.concatenate(parts_nrm).astype(np.float32),
        np.concatenate(parts_uv).astype(np.float32),
        np.concatenate(parts_ao).astype(np.float32),
        np.concatenate(parts_mat),
        materials,
    )


def _srgb_to_linear(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _linear_to_srgb(c):
    c = np.clip(c, 0.0, 1.0)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)


# --- rasteriser ----------------------------------------------------------------------

TILE = 16  # largest tile side, pixels


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
    def __init__(self, scene: Scene, camera: Camera, width: int, height: int):
        pts = scene.pos.reshape(-1, 3)
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        self.centre = (lo + hi) / 2
        length = float(max(hi - lo))
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
        self.width, self.height = width, height
        self.ground = float(lo[1])
        self.lo, self.hi = lo, hi

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


def _surface(scene: Scene, view: _View, sx, sy, sw, k: np.ndarray, tri: np.ndarray):
    """Interpolated attributes at pixels k on triangles tri: (normal, uv, ao, view ray)."""
    px, py = (k % view.width) + 0.5, (k // view.width) + 0.5
    ax, ay, bx, by, cx, cy = sx[tri, 0], sy[tri, 0], sx[tri, 1], sy[tri, 1], sx[tri, 2], sy[tri, 2]
    a = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    l0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / a
    l1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / a
    lam = np.stack((l0, l1, 1 - l0 - l1), axis=1) * sw[tri]
    lam /= np.maximum(lam.sum(axis=1, keepdims=True), 1e-12)
    n = np.einsum("nc,nci->ni", lam, scene.nrm[tri])
    n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-9)
    uv = np.einsum("nc,nci->ni", lam, scene.uv[tri])
    ao = np.einsum("nc,nc->n", lam, scene.ao[tri])
    ray = view.rays(k)
    # Double-sided: the normal faces the camera.
    flip = np.einsum("ni,ni->n", n, ray) > 0
    n[flip] *= -1
    return n, uv, ao, ray


def _shade(scene: Scene, view: _View, light, n, uv, ao, ray, mats) -> tuple[np.ndarray, np.ndarray]:
    """Linear colour of opaque surface points, and their Fresnel term."""
    out = np.zeros((len(n), 3), np.float32)
    fres = np.zeros(len(n), np.float32)
    # The baked term is Forza's AO times 0.25 + 0.55 (1 + n_y) / 2 (vehicle-model.md): undo the second factor.
    occlusion = np.clip(ao / (0.25 + 0.55 * (1 + n[:, 1]) / 2), 0.15, 1.0)
    for m in np.unique(mats):
        sel = np.flatnonzero(mats == m)
        mat = scene.materials[m]
        sh = SHADING[mat.kind]
        nn, rr = n[sel], ray[sel]
        if mat.kind in ("paint", "metallic") and mat.texture is not None:
            # tinted = tex.r * lerp(1, paint, tex.b) (lights.md, the paint shader): the
            # shared atlas is channel-packed, red the grey level, blue the paint mask.
            t = _sample(mat.texture, uv[sel])
            base = _srgb_to_linear(t[:, 0:1]) * (1 + (mat.colour[None] - 1) * t[:, 2:3])
        elif mat.packed and mat.texture is not None and mat.kind != "chrome":
            base = np.repeat(_srgb_to_linear(_sample(mat.texture, uv[sel])[:, 0:1]), 3, axis=1)
        elif mat.texture is not None and mat.kind not in ("chrome",):
            base = _srgb_to_linear(_sample(mat.texture, uv[sel])[:, :3])
        else:
            base = np.broadcast_to(mat.colour, (len(sel), 3))
        cos_v = np.clip(-np.einsum("ni,ni->n", nn, rr), 0, 1)
        f = sh.f0 + (1 - sh.f0) * (1 - cos_v) ** 5
        refl = rr - 2 * np.einsum("ni,ni->n", rr, nn)[:, None] * nn
        blur = refl * sh.gloss + nn * (1 - sh.gloss)
        blur /= np.maximum(np.linalg.norm(blur, axis=1, keepdims=True), 1e-9)
        env = _environment(blur)
        occ = occlusion[sel, None]
        ambient = (GROUND_AMBIENT + (SKY_AMBIENT - GROUND_AMBIENT) * (1 + nn[:, 1:2]) / 2) * occ
        lambert = np.clip(nn @ light, 0, None)[:, None] * KEY_POWER
        half = light[None] - rr
        half /= np.linalg.norm(half, axis=1, keepdims=True)
        spec = sh.highlight * np.clip(np.einsum("ni,ni->n", nn, half), 0, None) ** sh.power
        diffuse = base * (ambient + lambert * np.clip(occ * 1.6, 0, 1)) * sh.diffuse
        r = (f * sh.reflect)[:, None]
        if mat.kind == "chrome":
            colour = diffuse + env * base * r * occ + spec[:, None] * occ
        else:
            colour = diffuse * (1 - r) + env * r * np.clip(occ * 1.6, 0, 1) + spec[:, None] * occ
        out[sel] = colour
        fres[sel] = f
    return out, fres


def _shade_glass(view: _View, light, n, ray) -> tuple[np.ndarray, np.ndarray]:
    """(reflected light added on top, fraction of the light behind let through)."""
    sh = SHADING["glass"]
    cos_v = np.clip(-np.einsum("ni,ni->n", n, ray), 0, 1)
    f = sh.f0 + (1 - sh.f0) * (1 - cos_v) ** 5
    refl = ray - 2 * np.einsum("ni,ni->n", ray, n)[:, None] * n
    half = light[None] - ray
    half /= np.linalg.norm(half, axis=1, keepdims=True)
    spec = sh.highlight * np.clip(np.einsum("ni,ni->n", n, half), 0, None) ** sh.power
    add = _environment(refl) * f[:, None] * sh.reflect + spec[:, None] + GLASS_HAZE
    through = (1 - f)[:, None] * GLASS_TINT
    return add, through


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
    best = (-math.inf, STOCK_CAMERA)
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


def render(scene: Scene, camera: Camera = STOCK_CAMERA, width: int = 900, height: int = 480) -> np.ndarray:
    """(height, width, 4) uint8 straight-alpha RGBA: the car, its glass and its
    floor shadow on a transparent background."""
    view = _View(scene, camera, width, height)
    light = _light(view)
    sx, sy, sz = view.project(scene.pos)
    ok = sz.min(axis=1) > 1e-3
    sw = np.where(ok[:, None], 1 / np.maximum(sz, 1e-3), 0)
    size = width * height
    glass = scene.glass
    opaque_t = np.flatnonzero(ok & ~glass)
    glass_t = np.flatnonzero(ok & glass)
    k, w, t = _fragments(sx[opaque_t], sy[opaque_t], sw[opaque_t], width, height)
    depth, tri = _nearest(k, w, opaque_t[t], size)
    colour = np.zeros((size, 3), np.float32)
    alpha = np.zeros(size, np.float32)
    hit = np.flatnonzero(tri >= 0)
    if len(hit):
        n, uv, ao, ray = _surface(scene, view, sx, sy, sw, hit, tri[hit])
        c, _ = _shade(scene, view, light, n, uv, ao, ray, scene.mat[tri[hit]])
        colour[hit] = c
        alpha[hit] = 1
    # Floor shadow under everything else.
    pix = np.arange(size)
    shadow = _shadow(view, pix)
    empty = alpha == 0
    alpha[empty] = shadow[empty]
    # Glass: the nearest layer in front of the opaque surface, then one behind it.
    if len(glass_t):
        gk, gw, gt = _fragments(sx[glass_t], sy[glass_t], sw[glass_t], width, height)
        gt = glass_t[gt]
        front = gw > depth[gk]
        gk, gw, gt = gk[front], gw[front], gt[front]
        layers = []
        for _ in range(2):
            if not len(gk):
                break
            gdepth, gtri = _nearest(gk, gw, gt, size)
            on = np.flatnonzero(gtri >= 0)
            layers.append((on, gtri[on]))
            behind = gw < gdepth[gk] * (1 - 1e-4)
            gk, gw, gt = gk[behind], gw[behind], gt[behind]
        for on, gtri in reversed(layers):  # far layer first
            n, _, _, ray = _surface(scene, view, sx, sy, sw, on, gtri)
            add, through = _shade_glass(view, light, n, ray)
            # Premultiplied: what is behind (or nothing) dimmed by the glass, plus its reflection.
            a = alpha[on]
            new_a = 1 - (1 - a) * through.mean(axis=1)
            premul = colour[on] * a[:, None] * through + add
            colour[on] = premul / np.maximum(new_a[:, None], 1e-6)
            alpha[on] = new_a
    rgb = _linear_to_srgb(colour * EXPOSURE)
    out = np.zeros((size, 4), np.uint8)
    out[:, :3] = np.clip(rgb * 255 + 0.5, 0, 255).astype(np.uint8)
    out[:, 3] = np.clip(alpha * 255 + 0.5, 0, 255).astype(np.uint8)
    return out.reshape(height, width, 4)
