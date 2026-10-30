"""Previews and checks: needle surveys, gauge and eye-view renders, hole checks."""

from __future__ import annotations

import math
import struct
from pathlib import Path

from dsfforza.convert.cockpit.geometry import (
    DASHBOARD_INSTRUMENT,
    GAUGE_IDS,
    INTERIOR_MASK,
    SPEED_DIAL_PARAM,
    SPEED_FLOOR_PARAM,
    _add,
    _cross,
    _dot,
    _mean,
    _norm,
    _scale,
    _sub,
    quat_rotate,
    rotate_about,
)
from dsfforza.convert.cockpit.hierarchy import gauge_records, head_camera
from dsfforza.convert.cockpit.interior import local_material_kind

# --- previews -------------------------------------------------------------------------

GLASS_FLAG = 7  # MODL per-submodel value of glass submodels (vanilla interiors and exteriors)


def _entry_triangles(
    archive: Path,
    vehc_index: int,
    mask: int | None,
    textured: bool,
    flat=None,
    skip_glass: bool = False,
    instruments: dict | None = None,
):
    """[(pts, uvs, tex, colour)] for one VEHC entry, as dsf_materials._render wants.
    skip_glass leaves out submodels whose MODL value is GLASS_FLAG (the
    previews have no transparency). instruments: gauge id -> (pivot, axis,
    degrees) turns needle vertices (stride 20, semantic 8 = (u, v, id, 0))
    the way the engine does (gauge_pose)."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    _, mesh = vm.load(archive, vehc_index)
    textures, materials = {}, {}
    if textured:
        with archive.open("rb") as stream:
            root = read_chunk(stream, 0)
            for i in range(max(0, vehc_index - 2), min(len(root.entries), vehc_index + 3)):
                if root.entries[i].tag != "VEHC":
                    continue
                for e in child(child(root.entries[i], "MDXN"), "DRSP").children:
                    if e.tag == "TEXC":
                        textures[payload(stream, child(e, "UIDC"))[:8]] = dm.decode_dds(
                            payload(stream, child(e, "DXTC"))
                        )
                    elif e.tag == "MACO":
                        info = dm.material_info(stream, e)
                        materials[info["uid"]] = info
    subs = vm.lod_submodels(mesh, mask) if mask is not None else mesh.submodels
    flags = {n: x for part in mesh.parts for lod in part.lods for n, x in zip(lod.submodels, lod.extra)}
    out = []
    for sub in subs:
        if skip_glass and flags.get(mesh.submodels.index(sub)) == GLASS_FLAG:
            continue
        info = materials.get(sub.material)
        tex = textures.get(next(iter(info["textures"]))) if info and info["textures"] else None
        colour = flat or dm.FLAT.get(sub.material.hex(), (150, 150, 160))
        needle = instruments is not None and vb_needle(mesh, sub)
        for vb, tri in vm.triangles(mesh, sub):
            pts = [vm.position(vb, i) for i in tri]
            if needle:
                for k, i in enumerate(tri):
                    turn = instruments.get(int(round(vb.read(vb.element(8), i)[2])))
                    if turn:
                        pts[k] = rotate_about(pts[k], turn[0], turn[1], turn[2])
            uvs = [vb.read(vb.element(8), i)[:2] for i in tri] if tex else None
            out.append((pts, uvs, tex, colour))
    return out


def vb_needle(mesh, sub) -> bool:
    vb = mesh.vertex_buffers.get(sub.vertex_buffer)
    return bool(vb and vb.stride in (16, 20) and vb.element(8))


def dials_material(stream, interior) -> bytes | None:
    """UID of the interior's needle material, as host_interior picks it: its first
    local MACO named _Dials or alpha-tested. The digital dials' material (a copy of
    digital_dash.VANILLA_MATERIAL, also named _Dials) is only added after the host's
    materials, and only when the host has a needle material."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats.vehicle_mesh import child

    for e in child(child(interior, "MDXN"), "DRSP").children:
        if e.tag == "MACO":
            info = dm.material_info(stream, e)
            if local_material_kind(info) == "dials":
                return info["uid"]
    return None


def needle_gauge_ids(stream, interior, dials_only: bool = False) -> dict[float, int]:
    """Semantic-8 gauge id (component 2) -> vertex count over the interior's
    needle buffers (DashboardInstrument). Anything but the integer ids of the
    gauge table leaves a needle unturned: a later pass that writes UV1 into
    these buffers (damage.interior_uv1 did) silently breaks the gauges.

    With `dials_only`, only the vertices drawn with the needle material
    (dials_material) count. The digital dials (digital_dash) draw g_fDigiData
    vertices with ids 1..7 through the same declaration, sometimes from the same
    buffer as the needles (hosts 250, 293), but the engine reads those through no
    gauge record."""
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload

    drsp = child(child(interior, "MDXN"), "DRSP").children
    drawn = None  # buffer UID -> indices of the vertices the needle material draws
    if dials_only:
        dials = dials_material(stream, interior)
        indices = {}
        for e in drsp:
            if e.tag == "IXCK":
                uid, count, size = struct.unpack_from("<8sII", payload(stream, child(e, "IXHD")))
                if size == 2:
                    indices[uid] = payload(stream, child(e, "IXDT"))[: count * 2]
        drawn = {}
        for e in drsp:
            if e.tag != "SBMC" or dials is None:
                continue
            d = payload(stream, child(e, "SBBH"))
            if d[8:16] != dials or d[0x30:0x38] not in indices:
                continue
            (base,) = struct.unpack_from("<I", d, 0x20)
            first, count = struct.unpack_from("<II", d, 0x38)
            ib = indices[d[0x30:0x38]]
            drawn.setdefault(d[16:24], set()).update(base + i for i in struct.unpack_from(f"<{count}H", ib, first * 2))
    counts: dict[float, int] = {}
    for e in drsp:
        if e.tag != "VXCK":
            continue
        elements = vm.parse_vdcl(payload(stream, child(e, "VDCL")))
        e8 = next((x for x in elements if x.semantic == 8), None)
        if e8 is None or e8.name_hash != DASHBOARD_INSTRUMENT or e8.components < 3 or e8.type != 8:
            continue
        header = payload(stream, child(e, "VXHD"))
        count, stride = struct.unpack_from("<II", header, 8)
        data = payload(stream, child(e, "VXDT"))
        verts = range(count) if drawn is None else sorted(v for v in drawn.get(header[:8], ()) if v < count)
        for v in verts:
            gid = struct.unpack_from("<e", data, v * stride + e8.offset + 4)[0]
            counts[gid] = counts.get(gid, 0) + 1
    return counts


def needle_check(stream, interior, h) -> tuple[list[int], int]:
    """What would leave a needle of a built car unturned: the gauge ids the
    needles drawn with the interior's Dials material carry that hierarchy `h`
    has no gauge record for, and the vertices drawn with that material from a
    buffer whose declaration has no DashboardInstrument (the engine reads no
    gauge id). The digital dials' vertices need no record (needle_gauge_ids)."""
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload

    ids = needle_gauge_ids(stream, interior, dials_only=True)
    records = gauge_records(h)
    unturned = sorted(int(k) for k, n in ids.items() if n and k in GAUGE_IDS.values() and k not in records)
    drsp = child(child(interior, "MDXN"), "DRSP").children
    dials = dials_material(stream, interior)
    declared = {}
    for e in drsp:
        if e.tag == "VXCK":
            e8 = next((x for x in vm.parse_vdcl(payload(stream, child(e, "VDCL"))) if x.semantic == 8), None)
            declared[payload(stream, child(e, "VXHD"))[:8]] = e8 is not None and e8.name_hash == DASHBOARD_INSTRUMENT
    blind = 0
    for e in drsp:
        if e.tag == "SBMC" and dials is not None:
            d = payload(stream, child(e, "SBBH"))
            if d[8:16] == dials and not declared.get(d[16:24], True):
                blind += struct.unpack_from("<I", d, 0x3C)[0] // 3  # index count -> triangles
    return unturned, blind


def needle_survey(archive: Path, manifest: Path | None = None, cars: list[int] | None = None) -> list[dict]:
    """Per ported car of a built archive: the gauge ids its interior needles
    carry (semantic 8) and the ids its hierarchy has a gauge record for
    (gauge_records, as FUN_0088E310 keeps them). A needle id without a record
    is drawn and never turns. The manifest (default: the archive's
    .json) names the cars and gives the parked speed and tach needles (drawn
    static because the host interior has no needle material)."""
    import json

    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    m = json.loads((manifest or archive.with_suffix(".json")).read_text(encoding="utf-8"))
    rows = []
    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        for car in m["cars"]:
            sid = car["spool_id"]
            if cars and sid not in cars:
                continue
            kinds = upvh.entries_for(stream, root.entries, sid, None)
            h = upvh.parse(payload(stream, child(child(kinds[0], "NONR"), "UPVH")))
            ids = needle_gauge_ids(stream, kinds[1], dials_only=True) if 1 in kinds else {}
            unturned, blind = needle_check(stream, kinds[1], h) if 1 in kinds else ([], 0)
            interior = car.get("cockpit", {}).get("interior", {})
            rows.append(
                {
                    "spool_id": sid,
                    "media": car["media"],
                    "host": car["host"],
                    "needles": sorted(int(k) for k, n in ids.items() if n and k == int(k)),
                    "gauge_records": sorted(gauge_records(h)),
                    "unturned": unturned,
                    "no_gauge_id_triangles": blind,
                    "parked": [f for f in interior.get("parked_needles", []) if f in GAUGE_IDS],
                    "added": car.get("cockpit", {}).get("hierarchy", {}).get("gauges_added", []),
                }
            )
    return rows


def gauge_pose(upvh_data: bytes, vo3d_data: bytes, mph: float, rpm: float) -> dict[int, tuple]:
    """gauge id -> (pivot, axis, degrees) as the engine draws the needles at
    this speed and engine speed (FUN_00891670 inputs, FUN_008A2610 turn)."""
    from dsfforza.build import handling
    from dsfforza.formats import upvh

    h = upvh.parse(upvh_data)
    v = handling.VO3D.parse(vo3d_data)
    lo, hi = v.params.get(SPEED_FLOOR_PARAM, 0.0), v.params.get(SPEED_DIAL_PARAM, 255.0)
    inputs = {1: min(max(rpm, v.get(40)), v.get(42)) * 0.0002, 2: min(max(abs(mph), lo), hi) * 0.0125}
    out = {}
    for e in h.table("seats"):
        pivot = struct.unpack_from("<3f", e, 0)
        q = struct.unpack_from("<4f", e, 0x10)
        gid, sweep = struct.unpack_from("<II", e, 0x20)
        out[gid] = (pivot, _norm(quat_rotate(q, (0, 0, 1))), sweep * inputs.get(gid, 0.0))
    return out


def render_gauges(
    archive: Path,
    spool_id: int,
    png: Path,
    readings: list[tuple[float, float]],
    fov_deg: float = 28,
    size: tuple[int, int] = (480, 300),
) -> dict:
    """The gauge cluster from the head camera, one tile per (mph, rpm), with
    the needles turned as the engine turns them. Tiles go left to right."""
    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh.entries_for(stream, root.entries, spool_id, None)
        index = {k: root.entries.index(e) for k, e in kinds.items()}
        nonr = child(kinds[0], "NONR")
        upvh_data, vo3d_data = payload(stream, child(nonr, "UPVH")), payload(stream, child(nonr, "VO3D"))
    eye, _ = head_camera(upvh_data, vo3d_data)
    pivots = [struct.unpack_from("<3f", e) for e in upvh.parse(upvh_data).table("seats")]
    look = _norm(_sub(_mean(pivots), eye))
    tiles, info = [], {"eye": [round(c, 4) for c in eye], "readings": []}
    for n, (mph, rpm) in enumerate(readings):
        pose = gauge_pose(upvh_data, vo3d_data, mph, rpm)
        tris = _entry_triangles(archive, index[1], INTERIOR_MASK, textured=True, skip_glass=True, instruments=pose)
        tile = png.with_name(f"{png.stem}.tile{n}.png")
        render_eye_view(tris, eye, tile, size[0], size[1], fov_deg, pitch_deg=0, look=look)
        tiles.append(tile)
        info["readings"].append({"mph": mph, "rpm": rpm, "degrees": {g: round(p[2], 1) for g, p in pose.items()}})
    _compose([tiles], png)
    for tile in tiles:
        tile.unlink()
    info["png"] = str(png)
    return info


def _marker_tris(centre, size: float, colour) -> list:
    """An octahedron, for marking points in renders."""
    c = centre
    axes = [(size, 0, 0), (-size, 0, 0), (0, size, 0), (0, -size, 0), (0, 0, size), (0, 0, -size)]
    tris = []
    for a in axes[0:2]:
        for b in axes[2:4]:
            for d in axes[4:6]:
                tris.append(([_add(c, a), _add(c, b), _add(c, d)], None, None, colour))
    return tris


def render_eye_view(
    tris,
    eye,
    path: Path | None,
    width: int = 640,
    height: int = 400,
    fov_deg: float = 75,
    pitch_deg: float = -8,
    look=(0.0, 0.0, 1.0),
    mark_empty=None,
    unshaded: bool = False,
    alpha_test: bool = False,
):
    """Perspective view from `eye` looking along +z (DSF forward), z-buffered,
    textured where a texture is given; no back-face culling. With
    `mark_empty` (an RGB), pixels nothing covers get that colour; returns
    (pixels, number of empty pixels). `unshaded` writes texels/colours as
    they are (no fake light), for compositing glass offline; `alpha_test`
    skips texels with blue < 3 (no depth write), like the glass pixel
    shaders' texkill."""
    from dsfforza.formats import vehicle_mesh as vm

    f = 0.5 * width / math.tan(math.radians(fov_deg) / 2)
    cp, sp = math.cos(math.radians(pitch_deg)), math.sin(math.radians(pitch_deg))
    fwd = _norm((look[0], look[1] * cp + sp, look[2] * cp))
    right = _norm(_cross((0, 1, 0), fwd))  # DSF +x is left, so right = up x forward
    right = _scale(right, -1) if _dot(right, (1, 0, 0)) > 0 else right
    up = _cross(fwd, right) if _dot(_cross(fwd, right), (0, 1, 0)) > 0 else _cross(right, fwd)
    pixels = bytearray([24, 24, 28] * width * height)
    depth = [math.inf] * (width * height)
    light = _norm((0.3, 0.8, -0.4))
    for pts3, uvs, tex, colour in tris:
        cam = []
        for p in pts3:
            d = _sub(p, eye)
            cam.append((_dot(d, right), _dot(d, up), _dot(d, fwd)))
        if min(c[2] for c in cam) < 0.03:
            continue
        pts = [(width / 2 + f * c[0] / c[2], height / 2 - f * c[1] / c[2], c[2]) for c in cam]
        (px, py, pz), (qx, qy, qz), (rx, ry, rz) = pts
        area = (qx - px) * (ry - py) - (qy - py) * (rx - px)
        if abs(area) < 1e-9:
            continue
        n = _norm(_cross(_sub(pts3[1], pts3[0]), _sub(pts3[2], pts3[0])))
        shade = 1.0 if unshaded else 0.45 + 0.55 * abs(_dot(n, light))
        x0, x1 = max(int(min(p[0] for p in pts)), 0), min(int(max(p[0] for p in pts)) + 1, width)
        y0, y1 = max(int(min(p[1] for p in pts)), 0), min(int(max(p[1] for p in pts)) + 1, height)
        if x1 - x0 > 4 * width or y1 - y0 > 4 * height:
            continue
        iz = [1 / pz, 1 / qz, 1 / rz]
        for y in range(y0, y1):
            for x in range(x0, x1):
                w0 = ((qx - x) * (ry - y) - (qy - y) * (rx - x)) / area
                w1 = ((rx - x) * (py - y) - (ry - y) * (px - x)) / area
                w2 = 1 - w0 - w1
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                inv = w0 * iz[0] + w1 * iz[1] + w2 * iz[2]
                z = 1 / inv
                k = y * width + x
                if z >= depth[k]:
                    continue
                if tex:
                    tw, th, trgba = tex
                    a, b, c = w0 * iz[0] * z, w1 * iz[1] * z, w2 * iz[2] * z
                    u = a * uvs[0][0] + b * uvs[1][0] + c * uvs[2][0]
                    v = a * uvs[0][1] + b * uvs[1][1] + c * uvs[2][1]
                    o = (min(th - 1, max(0, int(v * th))) * tw + min(tw - 1, max(0, int(u * tw)))) * 4
                    col = trgba[o : o + 3]
                    if alpha_test and col[2] < 3:
                        continue
                else:
                    col = colour
                depth[k] = z
                o = k * 3
                pixels[o : o + 3] = bytes(min(255, int(ch * shade)) for ch in col)
    empty = sum(1 for d in depth if d == math.inf)
    if mark_empty is not None:
        for k, d in enumerate(depth):
            if d == math.inf:
                pixels[k * 3 : k * 3 + 3] = bytes(mark_empty)
    if path is not None:
        vm.write_png(path, width, height, pixels)
    return pixels, empty


def render(archive: Path, spool_id: int, png: Path | None, eye_png: Path | None, size: int = 360) -> dict:
    """Cutaway orthographic views and a perspective view from the head camera.

    Row 1: the interior entry (mask 64, textured, glass left out); row 2: the
    clean exterior (blue-grey) with the interior. Columns: side view from the
    passenger side with everything on the passenger side of the eye cut
    away; top view with everything above the eye cut away; rear view with
    everything behind the eye cut away. Yellow: eye (head camera), cyan:
    steering marker, magenta: gauge pivots."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import upvh
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh.entries_for(stream, root.entries, spool_id, None)
        index = {k: root.entries.index(e) for k, e in kinds.items()}
        nonr = child(kinds[0], "NONR")
        upvh_data = payload(stream, child(nonr, "UPVH"))
        eye, marker = head_camera(upvh_data, payload(stream, child(nonr, "VO3D")))
    h = upvh.parse(upvh_data)
    gauges = [struct.unpack_from("<3f", e) for e in h.table("seats")]
    info = {
        "eye": [round(c, 4) for c in eye],
        "steering_marker": [round(c, 4) for c in marker],
        "gauges": [[round(c, 4) for c in g] for g in gauges],
    }
    interior = _entry_triangles(archive, index[1], INTERIOR_MASK, textured=True, skip_glass=True)
    marks = (
        _marker_tris(eye, 0.05, (255, 230, 0))
        + _marker_tris(marker, 0.035, (0, 220, 255))
        + [t for g in gauges for t in _marker_tris(g, 0.02, (255, 0, 255))]
    )
    if png:
        exterior = _entry_triangles(archive, index[2], 2, textured=False, flat=(70, 80, 110), skip_glass=True)
        # (view, keep(centroid)) per column; DSF +x is the driver's left, +z forward.
        cuts = [
            (dm.VIEWS["side"], lambda c: c[0] >= eye[0] - 0.02),
            (dm.VIEWS["top"], lambda c: c[1] <= eye[1] - 0.02),
            (dm.VIEWS["front"], lambda c: c[2] >= eye[2] + 0.02),
        ]
        tiles = []
        for row in (interior, exterior + interior):
            line = []
            for n, (view, keep) in enumerate(cuts):
                tris = [t for t in row if keep(_mean(t[0]))] + marks
                tile = png.with_name(f"{png.stem}.tile{len(tiles)}{n}.png")
                dm._render(tris, tile, size, [view])
                line.append(tile)
            tiles.append(line)
        _compose(tiles, png)
        for line in tiles:
            for tile in line:
                tile.unlink()
        info["png"] = str(png)
    if eye_png:
        render_eye_view(interior + _marker_tris(marker, 0.01, (0, 220, 255)), eye, eye_png)
        info["eye_png"] = str(eye_png)
    return info


# Head positions (DSF model space offsets from the head camera; +x is the
# driver's left, +z forward) and look directions for the hole check. DSF's
# cockpit camera bobs and looks left and right.
HEAD_OFFSETS = [(0.0, 0.0, 0.0), (0.10, 0.0, 0.0), (-0.10, 0.0, 0.0), (0.0, 0.06, -0.06)]
HEAD_LOOKS = [
    ("ahead", (0.0, 0.0, 1.0), -8),
    ("left", (0.7, 0.0, 0.7), -10),
    ("right", (-0.7, 0.0, 0.7), -10),
    ("down", (0.0, 0.0, 1.0), -40),
    ("door-l", (1.0, 0.0, 0.25), -15),
    ("door-r", (-1.0, 0.0, 0.25), -15),
]
HOLE_COLOUR = (255, 0, 255)


def check_holes(
    archive: Path, spool_id: int, png: Path | None, size: tuple[int, int] = (240, 150), fov_deg: float = 90
) -> dict:
    """Render the interior (glass included, flat) from HEAD_OFFSETS x
    HEAD_LOOKS around the head camera; pixels nothing covers are holes (or
    open windows on open cars) and turn magenta. Returns the empty-pixel
    share per view and the worst."""
    from dsfforza.formats import upvh
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.chnk import read_chunk
    from dsfforza.formats.vehicle_mesh import child, payload

    with archive.open("rb") as stream:
        root = read_chunk(stream, 0)
        kinds = upvh.entries_for(stream, root.entries, spool_id, None)
        index = {k: root.entries.index(e) for k, e in kinds.items()}
        nonr = child(kinds[0], "NONR")
        eye, _ = head_camera(payload(stream, child(nonr, "UPVH")), payload(stream, child(nonr, "VO3D")))
    tris = _entry_triangles(archive, index[1], INTERIOR_MASK, textured=True, skip_glass=False)
    w, h = size
    rows, views = [], {}
    for offset in HEAD_OFFSETS:
        line = []
        for name, look, pitch in HEAD_LOOKS:
            pixels, empty = render_eye_view(
                tris, _add(eye, offset), None, w, h, fov_deg, pitch, look, mark_empty=HOLE_COLOUR
            )
            views[f"{name}@{','.join(f'{c:+.2f}' for c in offset)}"] = round(empty / (w * h), 4)
            line.append(pixels)
        rows.append(line)
    if png is not None:
        out = bytearray()
        for line in rows:
            for y in range(h):
                for pixels in line:
                    out += pixels[y * w * 3 : (y + 1) * w * 3]
        vm.write_png(png, w * len(HEAD_LOOKS), h * len(HEAD_OFFSETS), out)
    worst = max(views, key=views.get)
    return {"eye": [round(c, 4) for c in eye], "views": views, "worst": worst, "worst_share": views[worst]}


def _read_png_rows(path: Path) -> tuple[int, list[bytes]]:
    """(width, RGB rows) of a PNG written by vehicle_mesh.write_png."""
    import zlib

    data = path.read_bytes()
    w, h = struct.unpack(">II", data[16:24])
    pos, idat = 8, b""
    while pos < len(data):
        n = struct.unpack(">I", data[pos : pos + 4])[0]
        if data[pos + 4 : pos + 8] == b"IDAT":
            idat += data[pos + 8 : pos + 8 + n]
        pos += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 3 + 1
    return w, [raw[y * stride + 1 : (y + 1) * stride] for y in range(h)]


def _compose(grid: list[list[Path]], out: Path) -> None:
    """Join PNG tiles (same size) into one image, row by row."""
    from dsfforza.formats import vehicle_mesh as vm

    rows, width = [], 0
    for line in grid:
        tiles = [_read_png_rows(p) for p in line]
        width = sum(w for w, _ in tiles)
        for y in range(len(tiles[0][1])):
            rows.append(b"".join(r[y] for _, r in tiles))
    vm.write_png(out, width, len(rows), bytearray(b"".join(rows)))
