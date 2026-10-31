"""DSF's wheel speed-blur disc against the rim it stands for (research/forza-lamps-blur-re.md).

DSF's vanilla wheel parts (exterior entry) draw the sharp rim on GLOBAL_Wheel_Blur_LOD1 with
vertex semantic 9 byte 3 = 0 (fades out as the wheel spins), the disc on
GLOBAL_Wheel_Blur_SpokeDisc(_LOD1) or _Disc(_LOD1) at byte 3 = 2 (fades in), and what stays
(tyre, centre caps) at byte 3 = 1. The programs (`gpch.py dis 120` / `124`, the LOD1 pass 0):

    rim  (GPCH 124): rgb = light x tex.r                         alpha = opacity (1.15 - b)
    disc (GPCH 120): rgb = light x tex.r x g_vec4WheelBlurColour alpha = tex.b x opacity (2.5 b - 0.15)
    light = amb x ao x (lightmap.x 0.4 + 0.6) x hemisphere(N) + max(N.L, 0) x sun x shadow
            x (1 + 0.66 (ao - 1)); + specular and reflection, both x tex.g

`vanilla` measures, per vehicle, whether the disc as drawn equals the rotational mean of the
spinning rim as drawn: on rings round the axle, per angle the front-most surface seen along the
axle, (a) with the rim (byte 3 = 0 and 1 surfaces) and (b) with the disc at full fade over the
byte 3 = 1 surfaces left (the rim faded out). Albedo is the texel decoded from sRGB (red as the
grey level for the shared packed textures; the disc's red x g_vec4WheelBlurColour), its opacity
the decoded blue; "lit" multiplies by the vertex AO, the ambient term's factor.

    uv run python tools/re/dsf_blur.py vanilla [--ids 227,241] [--out out/re4/dsf-blur.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from dsfforza import REPO

sys.path.insert(0, str(Path(__file__).parent))

ARCHIVE = REPO / "out" / "vanilla" / "Install" / "dngvehicles.sp"
RINGS = 10  # radial bins, axle to the rim's outer radius
ANGLES = 90
# What a ring sample that meets no wheel-part surface shows (the brake, hub and arch are other
# parts): the same in the spinning rim and the faded-in disc, so it doesn't favour either.
BACKGROUND = np.zeros(3)


def srgb(c):
    c = np.asarray(c, float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def blur_colour(stream, full) -> tuple[float, float, float] | None:
    """g_vec4WheelBlurColour of the vehicle (SPSR in the full entry's NONR)."""
    from dsfforza.convert.paint import _current, parse_marp
    from dsfforza.formats.vehicle_mesh import child

    spsr = next((c for c in child(full, "NONR").children if c.tag == "SPSR"), None)
    for spss in spsr.children if spsr is not None else []:
        for spsp in spss.children or []:
            for p in spsp.children or []:
                if p.tag == "MARP":
                    data = _current(stream, p)
                    name, value, _ = parse_marp(data)
                    if name == "g_vec4WheelBlurColour":
                        import struct

                        return struct.unpack("<4f", value)[:3]
    return None


def wheel_parts(archive: Path, spool_id: int):
    """The exterior entry's wheel parts: list of (submodels as dicts: pos (n,3,3), uv, ao,
    byte3 (n,), material name, texture, packed), blur colour."""
    from dsfforza.convert import dsf_materials as dm
    from dsfforza.formats import upvh as uv
    from dsfforza.formats import vehicle_mesh as vm
    from dsfforza.formats.vehicle_mesh import child, payload
    from dsfforza.game import car_render as cr
    from dsfforza.game.gui_assets import _root_entries

    stream = archive.open("rb")
    entries = _root_entries(archive, stream)
    kinds = uv.entries_for(stream, entries, spool_id, None)
    index = {k: entries.index(v) for k, v in kinds.items()}
    shared_mats, shared_tex, decoded = cr._shared(archive, stream, entries)
    textures, infos = {}, {}
    for k in kinds:  # a port's atlas may sit in another entry's DRSP (car_render._load_scene)
        for e in child(child(kinds[k], "MDXN"), "DRSP").children:
            if e.tag == "TEXC":
                textures[payload(stream, child(e, "UIDC"))[:8]] = cr._decode_texture(stream, e)
            elif e.tag == "MACO":
                info = dm.material_info(stream, e)
                infos[info["uid"]] = info

    def texture_of(uid):
        info = infos.get(uid) or shared_mats.get(uid)
        if not info or not info["textures"]:
            return None, False, ""
        name = info["path"].lower().replace("\\", "/").rsplit("/", 1)[-1]
        t = next(iter(info["textures"]))
        if t in textures:
            return textures[t], False, name
        if t in shared_tex:
            if t not in decoded:
                decoded[t] = cr._decode_texture(stream, shared_tex[t])
            return decoded[t], True, name
        return None, False, name

    colour = blur_colour(stream, kinds[0])
    _, mesh = vm.load(archive, index[2], entries)
    cache = {}
    parts = []
    for part in mesh.parts:
        subs = [mesh.submodels[n] for lod in part.lods for n in lod.submodels]
        if not any(mesh.vertex_buffers[s.vertex_buffer].stride == 20 or s.material in cr.TYRE_UIDS for s in subs):
            continue
        out, seen = [], set()
        for sub in subs:
            key = (sub.vertex_buffer, sub.index_buffer, sub.first_index, sub.index_count, sub.base_vertex)
            if key in seen:
                continue
            seen.add(key)
            pos, _nrm, uvs, ao = cr._submodel_triangles(mesh, sub, cache)
            s9 = cr._submodel_s9(mesh, sub, cache)
            byte3 = s9[:, :, 3].max(axis=1) if s9 is not None else np.ones(len(pos))
            tex, packed, name = texture_of(sub.material)
            out.append(
                {
                    "pos": pos,
                    "uv": uvs,
                    "ao": ao,
                    "byte3": byte3,
                    "name": name,
                    "tex": tex,
                    "packed": packed,
                    "tyre": sub.material in cr.TYRE_UIDS,
                }
            )
        parts.append(out)
    stream.close()
    return parts, colour


def _sample(tex, uv):
    h, w = tex.shape[:2]
    x = np.clip((uv[:, 0] % 1.0) * w, 0, w - 1).astype(int)
    y = np.clip((uv[:, 1] % 1.0) * h, 0, h - 1).astype(int)
    return tex[y, x]


def rings(subs, colour, axis: int = 0):
    """Per ring (radius bin) the rotational means of the spinning rim and of the faded-in disc
    (see the module docstring). Positions in the wheel part's frame, axle along `axis`, the
    outside toward +axis (the disc's side)."""
    o = [i for i in range(3) if i != axis]
    tris = []  # (pos (n,3,3), uv (n,3,2), ao (n,3), class, sub)
    for s in subs:
        for cls in (0, 1, 2):
            k = s["byte3"] == cls
            if k.any():
                tris.append((s["pos"][k], s["uv"][k], s["ao"][k], cls, s))
    disc = [t for t in tris if t[3] == 2 and not t[4]["tyre"]]
    if not disc:
        return None
    rim_r = max(np.linalg.norm(t[0][..., o], axis=-1).max() for t in disc)
    r_edges = np.linspace(0, rim_r, RINGS + 1)
    rr = (r_edges[:-1] + r_edges[1:]) / 2
    th = (np.arange(ANGLES) + 0.5) / ANGLES * 2 * math.pi
    R, T = np.meshgrid(rr, th, indexing="ij")
    pts = np.stack([R.ravel() * np.cos(T.ravel()), R.ravel() * np.sin(T.ravel())], 1)
    n = len(pts)
    # per sample: the hits of every surface, (depth along +axis, albedo rgb, alpha, ao, class)
    hits = [[] for _ in range(n)]
    for pos, uvs, ao, cls, s in tris:
        p2 = pos[..., o]
        a, b, c = p2[:, 0], p2[:, 1], p2[:, 2]
        d = (b[:, 1] - c[:, 1]) * (a[:, 0] - c[:, 0]) + (c[:, 0] - b[:, 0]) * (a[:, 1] - c[:, 1])
        ok = np.abs(d) > 1e-12
        for t in np.nonzero(ok)[0]:
            lo, hi = p2[t].min(0), p2[t].max(0)
            sel = np.nonzero((pts[:, 0] >= lo[0]) & (pts[:, 0] <= hi[0]) & (pts[:, 1] >= lo[1]) & (pts[:, 1] <= hi[1]))[
                0
            ]
            if not len(sel):
                continue
            q = pts[sel]
            l1 = ((b[t, 1] - c[t, 1]) * (q[:, 0] - c[t, 0]) + (c[t, 0] - b[t, 0]) * (q[:, 1] - c[t, 1])) / d[t]
            l2 = ((c[t, 1] - a[t, 1]) * (q[:, 0] - c[t, 0]) + (a[t, 0] - c[t, 0]) * (q[:, 1] - c[t, 1])) / d[t]
            l3 = 1 - l1 - l2
            inside = (l1 >= 0) & (l2 >= 0) & (l3 >= 0)
            if not inside.any():
                continue
            w = np.stack([l1, l2, l3], 1)[inside]
            depth = w @ pos[t, :, axis]
            uv = w @ uvs[t]
            aoi = w @ ao[t]
            if s["tex"] is None:
                rgba = np.tile([0.3, 0.3, 0.3, 1.0], (len(uv), 1))
            else:
                rgba = _sample(s["tex"], uv)
            if cls == 2:
                alb = srgb(rgba[:, 0])[:, None] * np.asarray(colour or (1, 1, 1))[None]
                alpha = srgb(rgba[:, 2])
            elif s["packed"] or s["tyre"]:
                alb = np.repeat(srgb(rgba[:, 0])[:, None], 3, 1)
                alpha = np.ones(len(uv))
            else:
                alb = srgb(rgba[:, :3])
                alpha = np.ones(len(uv))
            for j, k in enumerate(sel[inside]):
                hits[k].append((depth[j], alb[j], alpha[j], aoi[j], cls))
    spin = np.full((n, 3), np.nan)
    spin_lit = np.full((n, 3), np.nan)
    faded = np.full((n, 3), np.nan)
    faded_lit = np.full((n, 3), np.nan)
    disc_alpha = np.full(n, np.nan)
    rim_hit = np.array([any(h[4] in (0, 1) for h in hs) for hs in hits], float)
    for k, hs in enumerate(hits):
        if not hs:
            spin[k] = spin_lit[k] = faded[k] = faded_lit[k] = BACKGROUND
            continue
        hs.sort(key=lambda h: -h[0])  # outside (+axis) first
        front = next((h for h in hs if h[4] in (0, 1)), None)
        spin[k], spin_lit[k] = (front[1], front[1] * front[3]) if front is not None else (BACKGROUND, BACKGROUND)
        under = next((h for h in hs if h[4] == 1), None)
        dsc = next((h for h in hs if h[4] == 2), None)
        if dsc is not None and (under is None or under[0] <= dsc[0]):
            a = dsc[2]
            ub = under[1] if under is not None else BACKGROUND
            ul = under[1] * under[3] if under is not None else BACKGROUND
            faded[k] = a * dsc[1] + (1 - a) * ub  # the blend; DSF's target is linear (A16B16G16R16F)
            faded_lit[k] = a * dsc[1] * dsc[3] + (1 - a) * ul
            disc_alpha[k] = a
        elif under is not None:
            faded[k], faded_lit[k] = under[1], under[1] * under[3]
        else:
            faded[k] = faded_lit[k] = BACKGROUND

    def ring_mean(v):
        v = v.reshape(RINGS, ANGLES, -1)
        cov = np.isfinite(v[..., 0]).mean(1)
        m = np.where(cov[:, None] > 0, np.nanmean(np.where(np.isfinite(v), v, np.nan), axis=1), np.nan)
        return m, cov

    def lum(v):
        return v @ np.array([0.2126, 0.7152, 0.0722])

    s_m, _ = ring_mean(spin)
    s_cov = rim_hit.reshape(RINGS, ANGLES).mean(1)
    f_m, f_cov = ring_mean(faded)
    sl_m, _ = ring_mean(spin_lit)
    fl_m, _ = ring_mean(faded_lit)
    da, _ = ring_mean(disc_alpha[:, None])
    rows = []
    for i in range(RINGS):
        rows.append(
            {
                "r": round(float(rr[i]), 3),
                "spin_cov": round(float(s_cov[i]), 2),
                "disc_cov": round(float(f_cov[i]), 2),
                "spin_lum": None if np.isnan(s_m[i]).any() else round(float(lum(s_m[i])), 4),
                "disc_lum": None if np.isnan(f_m[i]).any() else round(float(lum(f_m[i])), 4),
                "spin_lum_ao": None if np.isnan(sl_m[i]).any() else round(float(lum(sl_m[i])), 4),
                "disc_lum_ao": None if np.isnan(fl_m[i]).any() else round(float(lum(fl_m[i])), 4),
                "disc_alpha": None if np.isnan(da[i, 0]) else round(float(da[i, 0]), 3),
            }
        )
    return {"rim_radius": round(float(rim_r), 3), "rings": rows}


def vehicle(spool_id: int, archive: Path = ARCHIVE) -> dict:
    parts, colour = wheel_parts(archive, spool_id)
    out = {"spool_id": spool_id, "blur_colour": colour, "parts": []}
    for subs in parts:
        disc = [s for s in subs if (s["byte3"] == 2).any() and not s["tyre"]]
        if not disc:
            continue
        # the axle: the axis along which the disc is thinnest
        p = np.concatenate([s["pos"][s["byte3"] == 2].reshape(-1, 3) for s in disc])
        axis = int(np.argmin(p.max(0) - p.min(0)))
        sign = 1.0 if p[:, axis].mean() >= 0 else -1.0
        if sign < 0:  # the disc's side toward +axis
            subs = [dict(s, pos=s["pos"] * np.where(np.arange(3) == axis, -1.0, 1.0)) for s in subs]
        res = rings(subs, colour, axis)
        if res:
            res["axis"] = axis
            res["materials"] = sorted({f"{s['name']} b3={sorted(set(s['byte3'].astype(int).tolist()))}" for s in subs})
            out["parts"].append(res)
    return out


def summary(rows: list[dict]) -> dict:
    """Per vehicle part: the radius-weighted ratio disc / spinning rim (luminance) over the rings
    where the disc shows, and the per-ring ratios' spread."""
    out = []
    for v in rows:
        for p in v.get("parts", []):
            num = den = numa = dena = 0.0
            ratios = []
            for g in p["rings"]:
                if g["spin_lum"] is None or g["disc_lum"] is None or g["disc_alpha"] is None:
                    continue  # rings where the disc doesn't show (a cap in front of it in both)
                num += g["r"] * g["disc_lum"]
                den += g["r"] * g["spin_lum"]
                numa += g["r"] * g["disc_lum_ao"]
                dena += g["r"] * g["spin_lum_ao"]
                if g["spin_lum"] > 1e-4:
                    ratios.append(g["disc_lum"] / g["spin_lum"])
            if den > 0 and ratios:
                out.append(
                    {
                        "spool_id": v["spool_id"],
                        "ratio": round(num / den, 3),
                        "ratio_ao": round(numa / dena, 3) if dena else None,
                        "ring_ratio_min": round(min(ratios), 3),
                        "ring_ratio_max": round(max(ratios), 3),
                        "blur_colour": v["blur_colour"],
                    }
                )
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("vanilla")
    v.add_argument("--ids")
    v.add_argument("--archive", type=Path, default=ARCHIVE)
    v.add_argument("--out", type=Path, default=REPO / "out" / "re4" / "dsf-blur.json")
    args = ap.parse_args(argv)
    if args.ids:
        ids = [int(x) for x in args.ids.split(",")]
    else:
        lines = (REPO / "research" / "vehicles.tsv").read_text().splitlines()[1:]
        ids = sorted({int(ln.split("\t")[1]) for ln in lines if ln.strip()})
    rows = json.loads(args.out.read_text()) if args.out.exists() else []
    done = {r["spool_id"] for r in rows}
    for sid in ids:
        if sid in done:
            continue
        try:
            rows.append(vehicle(sid, args.archive))
        except Exception as e:  # a vehicle without wheels (boats, trains): recorded
            rows.append({"spool_id": sid, "error": f"{type(e).__name__}: {e}"})
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows))
    for s in summary(rows):
        print(json.dumps(s))


if __name__ == "__main__":
    main()
