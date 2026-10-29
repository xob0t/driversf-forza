"""Every ported car against Forza's own look: renders, region scores, a contact sheet.

For each car of a build, the entries the game draws (the clean exterior the player
drives, the full entry's finest LOD that traffic shows, and later the interior from
the driver's eye) are rendered from the built `dngvehicles.sp` with their own DSF
materials, textures and lamps (car_render, physical), next to a reference render of
the same Forza LOD with Forza's materials (forza_render), at the same cameras, by day,
with the lamps lit by day and in the dark mood. Both renders keep which triangle each
pixel shows (car_render.Frame); every built triangle is matched to the Forza triangle
it was made from (render_match), so both ID buffers say which Forza section, technique,
region and lamp function each pixel is. Per region the scores compare coverage,
brightness and colour, detail (structure and contrast: "painted on" is detail
collapsing), what draws it on our side (the DSF program) and, in the lit modes, the
glow; flags above calibrated limits rank the roster (research/render-compare.md).

    uv run dsff render_compare run --build out/build --out out/rc/master [--cars A,B] [--jobs 4]
    uv run dsff render_compare sheet out/rc/master
    uv run dsff render_compare diff --before out/build --after out/pr/build --out out/rc/pr [--jobs 4]

`diff` is render_diff: only the cars whose drawn data differ are rendered.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from dsfforza.game import car_render as cr
from dsfforza.harness import forza_render as fr

# Region ids of the ID maps (0: nothing drawn).
REGIONS = [
    "none",
    fr.BODY,
    fr.TRIMS,
    fr.CHROME,
    fr.GLASS,
    fr.PLATES,
    fr.BADGES,
    fr.WHEELS,
    fr.FRONT_LAMPS,
    fr.REAR_LAMPS,
    "cabin",
    fr.COCKPIT,
    fr.GAUGES,
    "other",
    fr.DISPLAYS,
]
RID = {name: i for i, name in enumerate(REGIONS)}
SCORED = [
    fr.BODY,
    fr.TRIMS,
    fr.CHROME,
    fr.PLATES,
    fr.BADGES,
    fr.WHEELS,
    fr.FRONT_LAMPS,
    fr.REAR_LAMPS,
    fr.COCKPIT,
    fr.GAUGES,
]
LAMP_REGIONS = (fr.FRONT_LAMPS, fr.REAR_LAMPS)
# DSF programs (car_render._physical) and Forza roles, for the "drawn as" maps.
PROGRAMS = ["none", "paint", "trim", "chrome", "tyre", "chassis", "lights", "gloss", "glass", "forza", "cabin"]
PID = {name: i for i, name in enumerate(PROGRAMS)}


@dataclass(frozen=True)
class View:
    name: str
    camera: cr.Camera
    zoom: str = ""  # "front" / "rear": frame that end of the car only (ZOOM_DEPTH)
    modes: tuple[str, ...] = ("day", "day_lit", "dark")


ZOOM_DEPTH = 0.75  # m of the car's end the front and rear views frame
VIEWS = {
    v.name: v
    for v in (
        View("front3q", cr.Camera(36.5, 13.0, 1.3)),
        View("rear3q", cr.Camera(143.5, 13.0, 1.3)),
        View("side", cr.Camera(90.0, 4.0, 1.35), modes=("day",)),
        View("front", cr.Camera(12.0, 7.0, 1.6), "front"),
        View("rear", cr.Camera(168.0, 7.0, 1.6), "rear"),
    )
}
DEFAULT_VIEWS = ("front3q", "rear3q", "side", "front", "rear")
SIZE = (800, 450)


# --- one car -------------------------------------------------------------------------------


def car_report(build: Path, media: str) -> dict | None:
    """The converter's report for a car of a build (port_batch's work/<media>.json)."""
    path = Path(build) / "work" / f"{media}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def roster(build: Path) -> list[dict]:
    """[{media, spool_id, game}] of a build's cars: batch-report.json, else (a build whose
    batch report is gone) its per-car reports, work/<media>.json."""
    path = Path(build) / "batch-report.json"
    if path.exists():
        rep = json.loads(path.read_text(encoding="utf-8"))
        return [{"media": c["media"], "spool_id": c["spool_id"], "game": c.get("game")} for c in rep.get("ok", [])]
    out = []
    for p in sorted((Path(build) / "work").glob("*.json")):
        if p.name.endswith((".job.json", ".fp.json")):
            continue
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if r.get("ok") and r.get("spool_id") is not None:
            out.append({"media": r["media"], "spool_id": r["spool_id"], "game": r.get("game")})
    return out


def _cache_dir(build: Path, media: str, report: dict | None) -> Path:
    if report and report.get("entry", {}).get("cache"):
        p = Path(report["entry"]["cache"])
        if p.exists():
            return p
    from dsfforza import REPO

    for game in ("fh1", "fm4"):
        p = REPO / "out" / "cache" / game / media
        if p.exists():
            return p
    raise FileNotFoundError(f"{media}: no Forza cache folder")


def plate_triangles(scene: cr.Scene) -> np.ndarray:
    """(T,) bool: the built car's licence plates (paint quads on the shared atlas's plate,
    plates.PLATE_UV)."""
    from dsfforza.convert import plates

    u0, v0, u1, v1 = plates.PLATE_UV
    paint = np.array([m.kind in cr.PAINT_KINDS for m in scene.materials])[scene.mat]
    uv = scene.uv
    inside = (
        (uv[:, :, 0] >= u0 - 1e-3)
        & (uv[:, :, 0] <= u1 + 1e-3)
        & (uv[:, :, 1] >= v0 - 1e-3)
        & (uv[:, :, 1] <= v1 + 1e-3)
    )
    return paint & inside.all(axis=1)


@dataclass
class Pair:
    """Our scene and the reference, with per-triangle region, lamp function and program."""

    ours: cr.Scene
    ref: cr.Scene
    ours_region: np.ndarray
    ref_region: np.ndarray
    ours_program: np.ndarray
    ref_origin: list
    ours_src: np.ndarray  # matched reference triangle, -1 none
    box: tuple
    match: dict


def _program_ids(scene: cr.Scene) -> np.ndarray:
    per = np.array([PID.get(m.program, PID["forza"] if m.program.startswith("forza") else 0) for m in scene.materials])
    out = per[scene.mat]
    if scene.inside is not None:
        out = np.where(scene.inside, PID["cabin"], out)
    return out.astype(np.uint8)


def build_pair(archive: Path, roots, spool_id: int, media: str, report: dict, entry: str = "exterior") -> Pair:
    """The built entry and its Forza reference, matched."""
    from dsfforza.harness import render_match as rm

    rep = report["entry"]
    mask = None
    if entry == "full":
        mask = cr.FULL_MASK
        lod = int(rep["lods"]["full"][str(mask)])
    else:
        lod = int(rep["lods"]["exterior"])
    ours = cr.load_scene(archive, spool_id, roots, interior=True, entry=entry, mask=mask, physical=True)
    if ours.inside is not None and ours.lamp is not None:
        ours.lamp[ours.inside] = -1  # the cabin's own light bytes are no lamps of the outside
    placement = rep["placement"]
    daytime = any(lp.type == 0x1D for lp in ours.lamps or [])
    folder = _cache_dir(archive.parent, media, report)
    ref = fr.body_scene(folder, media, placement, lod, ours.info["paint"], ours.info["hubs"], daytime)
    wheel_part = ours.info["wheel"]
    cabin = ours.inside if ours.inside is not None else np.zeros(len(ours.mat), bool)
    body = ~cabin & ~wheel_part
    src = np.full(len(ours.mat), -1, np.int64)
    dist = np.full(len(ours.mat), np.inf)
    ref_body = ref.scene.tag != len(ref.origins) - 1  # the last origin is the wheel's
    ref_idx = np.flatnonzero(ref_body)
    s, d = rm.match(ours.pos[body], ref.scene.pos[ref_idx])
    src[body] = np.where(s >= 0, ref_idx[np.maximum(s, 0)], -1)
    dist[body] = d
    ref_region = np.array([RID[o.region] for o in ref.origins], np.uint8)[ref.scene.tag]
    ours_region = np.full(len(ours.mat), RID["other"], np.uint8)
    on = src >= 0
    ours_region[on] = ref_region[src[on]]
    prog = _program_ids(ours)
    ours_region[body & ~on & (prog == PID["glass"])] = RID[fr.GLASS]
    ours_region[plate_triangles(ours) & body] = RID[fr.PLATES]
    ours_region[wheel_part] = RID[fr.WHEELS]
    ours_region[cabin] = RID["cabin"]
    # The reference sees our cabin through its windows: the same triangles, so the cabin
    # never differs.
    if cabin.any():
        cab = ours.subset(cabin)
        cab.tag = np.full(int(cabin.sum()), len(ref.origins), np.int64)
        cab.lamp = np.full(int(cabin.sum()), -1, np.int32)
        ref_scene = cr.Scene.join([ref.scene, cab])
        origins = ref.origins + [fr.Origin("cabin", "cabin", "cabin")]
        ref_region = np.concatenate([ref_region, np.full(int(cabin.sum()), RID["cabin"], np.uint8)])
    else:
        ref_scene, origins = ref.scene, ref.origins
    pts = ours.pos[~cabin].reshape(-1, 3)
    box = (pts.min(axis=0), pts.max(axis=0))
    n_body = int(body.sum())
    match = {
        "triangles": n_body,
        "on": int((dist[body] <= rm.ON).sum()),
        "near": int(((dist[body] > rm.ON) & (src[body] >= 0)).sum()),
        "none": int((src[body] < 0).sum()),
        "plates": int((plate_triangles(ours) & body).sum()),
        "lod": lod,
    }
    return Pair(ours, ref_scene, ours_region, ref_region, prog, origins, src, box, match)


COCKPIT_LOOK = (0.0, -0.2, 1.0)  # forward and a little down, as DSF's cockpit camera at rest
COCKPIT_FOV = 75.0


def cockpit_camera(report: dict) -> cr.Camera | None:
    """The driver's eye of the built car (the converter's report: Forza's eye placed, raised
    by camera_rise as the cockpit camera is), looking ahead."""
    cp = report.get("entry", {}).get("cockpit")
    if not cp or "eye" not in cp:
        return None
    x, y, z = cp["eye"]
    rise = cp.get("gauge_sight", {}).get("camera_rise", 0.0) or 0.0
    return cr.Camera(0.0, 0.0, 0.0, eye=(x, y + rise, z), look=COCKPIT_LOOK, fov=COCKPIT_FOV)


def build_cockpit_pair(archive: Path, roots, spool_id: int, media: str, report: dict) -> Pair:
    """The interior entry (glass and needles too) and Forza's cockpit, matched."""
    from dsfforza.harness import render_match as rm

    rep = report["entry"]
    ours = cr.load_scene(archive, spool_id, roots, entry="interior", physical=True)
    ours.lamp = np.full(len(ours.mat), -1, np.int32)
    # The reference's shell at the finest LOD: the converter's coarser one can close the
    # cockpit (the Audi R10's lid, #157), which is what this view should find.
    folder = _cache_dir(archive.parent, media, report)
    ref, _ = fr.cockpit_scene(folder, media, rep["placement"], ours.info["paint"], 1)
    src, dist = rm.match(ours.pos, ref.scene.pos)
    ref_region = np.array([RID[o.region] for o in ref.origins], np.uint8)[ref.scene.tag]
    ours_region = np.full(len(ours.mat), RID["other"], np.uint8)
    on = src >= 0
    ours_region[on] = ref_region[src[on]]
    prog = _program_ids(ours)
    prog[:] = np.array([PID.get(m.program, 0) for m in ours.materials])[ours.mat]
    ours_region[~on & (prog == PID["glass"])] = RID[fr.GLASS]
    from dsfforza.convert import digital_dash

    live = np.array([m.uid == digital_dash.VANILLA_MATERIAL for m in ours.materials])[ours.mat]
    ours_region[live] = RID[fr.DISPLAYS]
    pts = ours.pos.reshape(-1, 3)
    match = {
        "triangles": len(ours.mat),
        "on": int((dist <= rm.ON).sum()),
        "near": int(((dist > rm.ON) & on).sum()),
        "none": int((~on).sum()),
        "source": ref.notes.get("source"),
    }
    return Pair(ours, ref.scene, ours_region, ref_region, prog, ref.origins, src, (pts.min(0), pts.max(0)), match)


# --- rendering -------------------------------------------------------------------------------


def _view_box(view: View, box) -> tuple:
    lo, hi = (np.array(b, np.float64) for b in box)
    if view.zoom == "front":
        lo[2] = hi[2] - ZOOM_DEPTH
    elif view.zoom == "rear":
        hi[2] = lo[2] + ZOOM_DEPTH
    return lo, hi


def region_map(scene: cr.Scene, frame: cr.Frame, region: np.ndarray) -> np.ndarray:
    """(h, w) region id per pixel: the nearest surface, seeing through window glass
    (lamp glass and the other see-through parts label their pixels)."""
    size = frame.view.width * frame.view.height
    out = np.zeros(size, np.uint8)
    hit = frame.tri >= 0
    out[hit] = region[frame.tri[hit]]
    for on, tri in reversed(frame.layers):  # far first, the nearest last
        r = region[tri]
        keep = r != RID[fr.GLASS]
        out[on[keep]] = r[keep]
    return out.reshape(frame.view.height, frame.view.width)


def tri_map(frame: cr.Frame, values: np.ndarray, see_through: bool = True) -> np.ndarray:
    """(h, w) per-pixel value of the opaque triangle (or of the nearest see-through layer)."""
    size = frame.view.width * frame.view.height
    out = np.zeros(size, values.dtype)
    hit = frame.tri >= 0
    out[hit] = values[frame.tri[hit]]
    if see_through:
        for on, tri in reversed(frame.layers):
            out[on] = values[tri]
    return out.reshape(frame.view.height, frame.view.width)


def union_box(*boxes) -> tuple:
    """The (lo, hi) corners holding every box: one frame for two builds of a car, so a part
    that moves outward (a plate 2 cm further out) doesn't shift every pixel (#254)."""
    lo = np.min([np.asarray(b[0], np.float64) for b in boxes], axis=0)
    hi = np.max([np.asarray(b[1], np.float64) for b in boxes], axis=0)
    return lo, hi


def render_view(pair: Pair, view: View, size=SIZE, box=None) -> dict:
    """Both scenes in one view, every mode: {"ours": {mode: linear rgb}, "ref": ...,
    "alpha", "regions", "programs"} (h, w) arrays. box: the car extent to frame instead of
    the pair's own (render_diff frames both builds alike)."""
    w, h = size
    box = _view_box(view, pair.box if box is None else box)
    out = {"ours": {}, "ref": {}, "alpha": {}, "regions": {}}
    for side, scene, region, rgb_of in (
        ("ours", pair.ours, pair.ours_region, cr.dsf_lamp_rgb),
        ("ref", pair.ref, pair.ref_region, fr.forza_lamp_rgb),
    ):
        v = cr._View(scene, view.camera, w, h, box=box)
        frame = cr.rasterize(scene, v)
        out["regions"][side] = region_map(scene, frame, region)
        if side == "ours":
            out["programs"] = tri_map(frame, pair.ours_program)
        for mode in view.modes:
            lt = cr.LIGHTINGS[mode]
            colour, alpha = cr.shade(scene, frame, lt, rgb_of(scene.lamps, lt), shadow=False)
            out[side][mode] = colour.reshape(h, w, 3)
            out["alpha"][side] = alpha.reshape(h, w)
            if mode == "dark":  # the dark mood with every lamp off: what the lamps add is the rest
                base, _ = cr.shade(scene, frame, lt, None, shadow=False)
                out[side]["dark_base"] = base.reshape(h, w, 3)
    return out


# --- scores ------------------------------------------------------------------------------------


def _lab(rgb_linear: np.ndarray) -> np.ndarray:
    """CIE L*a*b* (D65) of linear sRGB after the renderer's exposure, values clipped to 1."""
    c = np.clip(rgb_linear * cr.EXPOSURE, 0, 1)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.9505, 1.0, 1.089])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack((116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])), axis=-1)


def _box_blur(a: np.ndarray, r: int) -> np.ndarray:
    """Mean over a (2r + 1)^2 window (edges clamped), by cumulative sums."""
    if r <= 0:
        return a
    p = np.pad(a, r, mode="edge")
    c = np.cumsum(np.cumsum(p, axis=0), axis=1)
    c = np.pad(c, ((1, 0), (1, 0)))
    k = 2 * r + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def _erode(mask: np.ndarray, r: int = 1) -> np.ndarray:
    return _box_blur(mask.astype(np.float32), r) > 0.999


def _gradient(lum: np.ndarray) -> np.ndarray:
    gx = np.zeros_like(lum)
    gy = np.zeros_like(lum)
    gx[:, 1:-1] = lum[:, 2:] - lum[:, :-2]
    gy[1:-1] = lum[2:] - lum[:-2]
    return np.hypot(gx, gy) / 2


MIN_PIXELS = 150  # a region smaller than this in a view is not scored there
GLOW_ON = 0.08  # linear luminance a pixel's glow must reach to count as glowing
DETAIL_RADIUS = 3  # px: the local-contrast window (7 x 7)


CROP_PAD = 8  # px round a region's box: its windowed measures (blur, local contrast) see its edge


def _crop(*masks) -> tuple[slice, slice]:
    any_ = np.logical_or.reduce(masks)
    ys, xs = np.nonzero(any_)
    h, w = any_.shape
    return (
        slice(max(0, ys.min() - CROP_PAD), min(h, ys.max() + CROP_PAD + 1)),
        slice(max(0, xs.min() - CROP_PAD), min(w, xs.max() + CROP_PAD + 1)),
    )


def region_scores(r: dict, mode: str = "day") -> dict:
    """{region: metrics} for one view in one mode (render_view's output)."""
    ro_full, rf_full = r["regions"]["ours"], r["regions"]["ref"]
    lo_full, lf_full = _lab(r["ours"][mode]), _lab(r["ref"][mode])
    out = {}
    for name in SCORED:
        rid = RID[name]
        mo, mf = ro_full == rid, rf_full == rid
        n_o, n_f = int(mo.sum()), int(mf.sum())
        if max(n_o, n_f) < MIN_PIXELS:
            continue
        box = _crop(mo, mf)
        mo, mf = mo[box], mf[box]
        ro, rf, lo, lf, progs = ro_full[box], rf_full[box], lo_full[box], lf_full[box], r["programs"][box]
        if name in LAMP_REGIONS:
            # A lamp is seen through its cover on one side and uncovered or baked on the
            # other (plain glass covers are left out, lights.md #160): its pixels are either
            # side's, and missing means our car draws nothing there.
            both = mo | mf
            n_b = int(both.sum())
            m = {
                "ref_px": n_f,
                "ours_px": n_o,
                "missing": round(float((mf & (ro == 0)).sum()) / n_f, 3) if n_f else None,
                "extra": round(float((mo & (rf == 0)).sum()) / n_o, 3) if n_o else None,
            }
        else:
            both = mo & mf
            n_b = int(both.sum())
            m = {
                "ref_px": n_f,
                "ours_px": n_o,
                "missing": round(1 - n_b / n_f, 3) if n_f else None,
                "extra": round(1 - n_b / n_o, 3) if n_o else None,
            }
        # What the ref's pixels of this region show on our side.
        if n_f:
            under = ro[mf]
            share = np.bincount(under, minlength=len(REGIONS)) / n_f
            m["ours_as"] = {REGIONS[i]: round(float(s), 3) for i, s in enumerate(share) if s >= 0.05 and i != rid}
        if n_o:
            under = rf[mo]
            share = np.bincount(under, minlength=len(REGIONS)) / n_o
            m["over"] = {REGIONS[i]: round(float(s), 3) for i, s in enumerate(share) if s >= 0.05 and i != rid}
            ps = np.bincount(progs[mo], minlength=len(PROGRAMS)) / n_o
            m["drawn_as"] = {PROGRAMS[i]: round(float(s), 3) for i, s in enumerate(ps) if s >= 0.05}
        if n_b >= MIN_PIXELS:
            inner = _erode(both, 1)
            if inner.sum() < MIN_PIXELS // 2:
                inner = both
            a, b = lo[inner], lf[inner]
            m["L_ours"] = round(float(a[:, 0].mean()), 1)
            m["L_ref"] = round(float(b[:, 0].mean()), 1)
            m["dE"] = round(float(np.linalg.norm(a - b, axis=1).mean()), 1)
            m["chroma_ours"] = round(float(np.hypot(a[:, 1], a[:, 2]).mean()), 1)
            m["chroma_ref"] = round(float(np.hypot(b[:, 1], b[:, 2]).mean()), 1)
            # Detail: local contrast of L* inside the region (its own edges left out).
            go, gf = _gradient(lo[..., 0]), _gradient(lf[..., 0])
            so = _local_std(lo[..., 0], both)
            sf = _local_std(lf[..., 0], both)
            m["grad_ours"] = round(float(go[inner].mean()), 2)
            m["grad_ref"] = round(float(gf[inner].mean()), 2)
            m["contrast_ours"] = round(float(so[inner].mean()), 2)
            m["contrast_ref"] = round(float(sf[inner].mean()), 2)
            hf = lf[..., 0] - _masked_mean(lf[..., 0], both, 4)
            ho = lo[..., 0] - _masked_mean(lo[..., 0], both, 4)
            x, y = ho[inner], hf[inner]
            den = math.sqrt(float((x * x).sum() * (y * y).sum()))
            m["structure"] = round(float((x * y).sum() / den), 3) if den > 1e-6 else None
        out[name] = m
    return out


def _masked_mean(a: np.ndarray, mask: np.ndarray, r: int) -> np.ndarray:
    w = _box_blur(mask.astype(np.float32), r)
    s = _box_blur(np.where(mask, a, 0).astype(np.float32), r)
    return s / np.maximum(w, 1e-6)


def _local_std(a: np.ndarray, mask: np.ndarray, r: int = DETAIL_RADIUS) -> np.ndarray:
    """Standard deviation of a over the masked pixels of each pixel's window."""
    w = _box_blur(mask.astype(np.float32), r)
    s1 = _box_blur(np.where(mask, a, 0).astype(np.float32), r)
    s2 = _box_blur(np.where(mask, a * a, 0).astype(np.float32), r)
    mean = s1 / np.maximum(w, 1e-6)
    return np.sqrt(np.maximum(s2 / np.maximum(w, 1e-6) - mean * mean, 0))


def _lum(rgb: np.ndarray) -> np.ndarray:
    return rgb @ np.array([0.2126, 0.7152, 0.0722], np.float32)


def _glow(r: dict, side: str, mode: str) -> np.ndarray:
    """(h, w) light a side's lamps add in a lit mode, its brightest channel (a red lamp
    glows with little luminance): the day_lit render less the day render (the same frame
    and light, lamps off); the dark render itself (no other light to speak of)."""
    if mode == "day_lit":
        return np.maximum(r[side]["day_lit"] - r[side]["day"], 0).max(axis=-1)
    if f"{mode}_base" in r[side]:
        return np.maximum(r[side][mode] - r[side][f"{mode}_base"], 0).max(axis=-1)
    return r[side][mode].max(axis=-1)  # runs from before dark_base: the dark render itself


GLOW_FLOOR = 0.03  # linear luminance: a lamp pixel glowing less than this is unlit
GLOW_SHARE = 0.25  # of the region's 95th-percentile glow: the pixels that make the lamp's shape


def glow_scores(r: dict, mode: str) -> dict:
    """{lamp region: glow metrics} in a lit mode. Levels differ by design (the converter
    maps Forza's glow masks onto vanilla's alpha distribution: lights.md, "Vanilla lamp
    alpha"), so each side's glow is taken against its own: the lit shape is where a
    region glows at GLOW_SHARE of its 95th percentile or more (and over GLOW_FLOOR)."""
    out = {}
    ro, rf = r["regions"]["ours"], r["regions"]["ref"]
    go, gf = _glow(r, "ours", mode), _glow(r, "ref", mode)
    for name in LAMP_REGIONS:
        rid = RID[name]
        mo, mf = ro == rid, rf == rid
        if max(int(mo.sum()), int(mf.sum())) < MIN_PIXELS:
            continue
        po = float(np.percentile(go[mo], 95)) if mo.any() else 0.0
        pf = float(np.percentile(gf[mf], 95)) if mf.any() else 0.0
        lit_o = mo & (go > max(GLOW_FLOOR, GLOW_SHARE * po))
        lit_f = mf & (gf > max(GLOW_FLOOR, GLOW_SHARE * pf))
        m = {
            "ref_p95": round(pf, 3),
            "ours_p95": round(po, 3),
            "ref_lit_px": int(lit_f.sum()),
            "ours_lit_px": int(lit_o.sum()),
            "region_px": int(mf.sum()),
        }
        if lit_f.any() and lit_o.any():
            inter = int((lit_f & lit_o).sum())
            m["covered"] = round(inter / int(lit_f.sum()), 3)  # Forza's lit pixels ours lights too
            m["inside"] = round(inter / int(lit_o.sum()), 3)  # our lit pixels Forza lights too
            # Flat glow (white plates): spread of the glow over the lit pixels, ours over Forza's.
            so = float(np.std(go[lit_o]) / max(1e-6, np.mean(go[lit_o])))
            sf = float(np.std(gf[lit_f]) / max(1e-6, np.mean(gf[lit_f])))
            m["spread_ours"], m["spread_ref"] = round(so, 3), round(sf, 3)
            both = lit_f & lit_o
            if both.sum() >= 30:
                co = r["ours"][mode][both] - (r["ours"]["day"][both] if mode == "day_lit" else 0)
                cf = r["ref"][mode][both] - (r["ref"]["day"][both] if mode == "day_lit" else 0)
                m["green_red_ours"] = round(float(co[:, 1].sum() / max(1e-6, co[:, 0].sum())), 3)
                m["green_red_ref"] = round(float(cf[:, 1].sum() / max(1e-6, cf[:, 0].sum())), 3)
        out[name] = m
    # Glow off the lamps (trims, body): lit pixels whose region is no lamp.
    lamps = np.isin(ro, [RID[n] for n in LAMP_REGIONS]) | (ro == RID["cabin"]) | (ro == RID[fr.GLASS])
    lamps_f = np.isin(rf, [RID[n] for n in LAMP_REGIONS]) | (rf == RID["cabin"]) | (rf == RID[fr.GLASS])
    out["stray"] = {
        "ours_px": int(((go > max(GLOW_FLOOR, 0.15)) & ~lamps & (ro > 0)).sum()),
        "ref_px": int(((gf > max(GLOW_FLOOR, 0.15)) & ~lamps_f & (rf > 0)).sum()),
    }
    return out


# --- flags ---------------------------------------------------------------------------------------

# Limits (calibrated on known bad and good cars: research/render-compare.md, "Calibration").
LIMITS = {
    "missing": 0.35,
    # The cockpit seen from the eye: Forza's cabin pixels ours covers with something else
    # (the Audi R10s' shell lid, #157: 3.1% and 4.6% before the fix, 0.3% at most after
    # it and on the controls).
    "cockpit_missing": 0.02,  # share of Forza's region pixels our car doesn't draw as that region
    "extra": 0.35,
    "dark": 18.0,  # L* below Forza's
    "light": 18.0,
    # Trims: dark and near Forza's on the good builds (L* within +6), lit up when a gloss
    # material's lobe is too broad (#176: the Camaro ZL1's splitter +21, the Raptor's vents +10).
    "trims_light": 8.0,
    "colour": 25.0,  # mean delta E
    "detail": 0.5,  # share of Forza's local contrast lost
    "structure": 0.25,  # correlation of the region's fine structure below this
    "drawn_as": 0.3,  # share of a lamp region drawn by a program that can't draw lamps
    "glow_place": 0.3,  # share of Forza's lit lamp pixels ours lights (and of ours Forza's) below this
    "glow_hue": 1.2,
    # Our daytime glow's 95th percentile over this (linear; a 0x01 lamp's texel of alpha 1
    # glows 2): white plates by day (#167: GTI VR6 7.2, Evo VI 8.5 before, 1.6 after).
    "glare": 3.0,  # |log| of the ratio of the glow's green/red, ours against Forza's
    "flat_glow": 0.45,  # our glow spread under this share of Forza's
    "plate_over": 0.1,  # share of a plate over badges, lamps or chrome
}
WEIGHTS = {
    fr.FRONT_LAMPS: 3.0,
    fr.REAR_LAMPS: 3.0,
    fr.TRIMS: 1.5,
    fr.PLATES: 1.0,
    fr.BADGES: 1.0,
    fr.CHROME: 1.0,
    fr.BODY: 1.0,
    fr.WHEELS: 1.0,
}
LAMP_PROGRAMS = ("lights", "gloss", "chassis")
LAMP_VIEWS_SKIP = ("side",)  # lamps seen edge-on: not scored there
PLATE_NOT_ON = (fr.BADGES, fr.FRONT_LAMPS, fr.REAR_LAMPS, fr.CHROME)  # what can draw a lamp's texels (Lights: glow)


def _flag(flags: list, region: str, view: str, what: str, value: float, severity: float, text: str) -> None:
    flags.append(
        {
            "region": region,
            "view": view,
            "what": what,
            "value": round(float(value), 3),
            "severity": round(float(severity) * WEIGHTS.get(region, 1.0), 3),
            "text": text,
        }
    )


def flags_for(metrics: dict) -> list[dict]:
    """Flags from a car's metrics ({view: {mode: {region: m}}}, glow under "glow_<mode>"),
    each with a severity; the worst view per (region, what) is kept."""
    flags: list = []
    L = LIMITS
    for view, modes in metrics.items():
        day = modes.get("day", {})
        for region, m in day.items():
            if region in LAMP_REGIONS and view in LAMP_VIEWS_SKIP:
                continue
            limit = L["cockpit_missing"] if region == fr.COCKPIT else L["missing"]
            if m.get("missing") is not None and m["ref_px"] >= MIN_PIXELS * 2 and m["missing"] > limit:
                # (the cockpit: weighted down: the reference's shell is the finest LOD, ours the
                # converter's coarser one, so the bonnet seen through the windscreen differs on
                # many cars by design; research/render-compare.md, "Open")
                severity = m["missing"] * (5 if region == fr.COCKPIT else 1)
                text = (
                    f"{region}: {m['missing']:.0%} not drawn"
                    if region != fr.COCKPIT
                    else (f"cockpit: {m['missing']:.0%} covered by something Forza's hasn't")
                )
                _flag(flags, region, view, "missing", m["missing"], severity, text)
            if (
                m.get("extra") is not None
                and region != fr.PLATES
                and m["ours_px"] >= MIN_PIXELS * 2
                and m["extra"] > L["extra"]
            ):
                _flag(flags, region, view, "extra", m["extra"], m["extra"] * 0.7, f"{region}: {m['extra']:.0%} extra")
            if "L_ours" in m:
                d = m["L_ref"] - m["L_ours"]
                if d > L["dark"]:
                    _flag(
                        flags, region, view, "dark", d, d / 40, f"{region}: too dark (L* {m['L_ours']} vs {m['L_ref']})"
                    )
                elif -d > (L["trims_light"] if region == fr.TRIMS else L["light"]):
                    _flag(
                        flags,
                        region,
                        view,
                        "light",
                        -d,
                        -d / (15 if region == fr.TRIMS else 40),
                        f"{region}: washed out (L* {m['L_ours']} vs {m['L_ref']})",
                    )
                if m["dE"] > L["colour"] and abs(d) < L["dark"]:
                    _flag(flags, region, view, "colour", m["dE"], m["dE"] / 60, f"{region}: colour off (dE {m['dE']})")
                if m["contrast_ref"] > 2.0:
                    lost = 1 - m["contrast_ours"] / m["contrast_ref"]
                    if lost > L["detail"]:
                        _flag(flags, region, view, "detail", lost, lost, f"{region}: detail lost {lost:.0%}")
                s = m.get("structure")
                if s is not None and s < L["structure"] and m["contrast_ref"] > 4.0 and m["contrast_ours"] > 2.0:
                    _flag(
                        flags,
                        region,
                        view,
                        "structure",
                        s,
                        (L["structure"] - s) * 1.5,
                        f"{region}: different detail (structure {s:.2f})",
                    )
            if region in LAMP_REGIONS and m.get("drawn_as"):
                bad = sum(v for k, v in m["drawn_as"].items() if k not in LAMP_PROGRAMS)
                if bad > L["drawn_as"]:
                    kinds = ", ".join(f"{k} {v:.0%}" for k, v in m["drawn_as"].items() if k not in LAMP_PROGRAMS)
                    _flag(flags, region, view, "drawn_as", bad, bad * 1.2, f"{region}: drawn as {kinds}")
            if region == fr.PLATES and m.get("over"):
                # A plate on a trim may be on its holder; on a badge, a lamp or chrome it isn't.
                over = sum(v for k, v in m["over"].items() if k in PLATE_NOT_ON)
                if over > L["plate_over"]:
                    where = ", ".join(f"{k} {v:.0%}" for k, v in m["over"].items() if k != fr.BODY)
                    _flag(flags, region, view, "plate_over", over, over, f"plates: over {where}")
        for mode in ("day_lit", "dark"):
            words = mode.replace("_", " ")
            for region, g in modes.get(f"glow_{mode}", {}).items():
                if region == "stray":
                    if g["ours_px"] > 300 and g["ours_px"] > 3 * g["ref_px"]:
                        _flag(
                            flags,
                            fr.TRIMS,
                            view,
                            f"stray_glow_{mode}",
                            g["ours_px"],
                            0.5,
                            f"glow off the lamps ({words}): {g['ours_px']} px",
                        )
                    continue
                if view in LAMP_VIEWS_SKIP:
                    continue
                ref_lit = g["ref_p95"] > 2 * GLOW_FLOOR and g["ref_lit_px"] >= 40
                if ref_lit and g["ours_p95"] < GLOW_FLOOR:
                    _flag(flags, region, view, f"no_glow_{mode}", g["ours_p95"], 1.0, f"{region}: no glow {words}")
                    continue
                if ref_lit and "covered" in g and max(g["covered"], g["inside"]) < L["glow_place"]:
                    v = max(g["covered"], g["inside"])
                    _flag(
                        flags,
                        region,
                        view,
                        f"glow_place_{mode}",
                        v,
                        0.6 * (1 - v),
                        f"{region}: glows elsewhere than Forza's lamp ({words}, {g['covered']:.0%} of it)",
                    )
                if mode == "day_lit" and g["ours_p95"] > L["glare"] and g["ours_lit_px"] >= 60:
                    _flag(
                        flags,
                        region,
                        view,
                        "glare_day_lit",
                        g["ours_p95"],
                        min(2.0, g["ours_p95"] / L["glare"]),
                        f"{region}: white plate by day (glow {g['ours_p95']:.1f}x a 0x01 lamp's full alpha)",
                    )
                if mode == "day_lit" and g.get("spread_ref") and g["ours_lit_px"] >= 60:
                    flat = g["spread_ours"] / g["spread_ref"]
                    if flat < L["flat_glow"]:
                        _flag(
                            flags,
                            region,
                            view,
                            f"flat_glow_{mode}",
                            flat,
                            0.6 * (1 - flat),
                            f"{region}: flat glow by day ({flat:.0%} of Forza's spread)",
                        )
                if "green_red_ours" in g:
                    a_, b_ = max(g["green_red_ours"], 0.02), max(g["green_red_ref"], 0.02)
                    off = abs(math.log(a_ / b_))
                    if off > L["glow_hue"]:
                        _flag(
                            flags,
                            region,
                            view,
                            f"glow_hue_{mode}",
                            off,
                            0.4 * min(off, 3),
                            f"{region}: glows in another colour ({words})",
                        )
    best: dict = {}
    for f in flags:
        key = (f["region"], f["what"])
        if key not in best or f["severity"] > best[key]["severity"]:
            best[key] = f
    return sorted(best.values(), key=lambda f: -f["severity"])


def car_score(flags: list[dict]) -> float:
    return round(sum(f["severity"] for f in flags), 3)


# --- images -------------------------------------------------------------------------------

BACKDROP = 58  # sRGB grey behind the cars


def to_srgb8(rgb_linear: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    srgb = cr._linear_to_srgb(rgb_linear * cr.EXPOSURE) * 255
    a = alpha[..., None]
    return np.clip(srgb * a + BACKDROP * (1 - a) + 0.5, 0, 255).astype(np.uint8)


def outline(img: np.ndarray, mask: np.ndarray, colour=(255, 60, 200)) -> None:
    """Draw mask's 1-px border onto img in place."""
    edge = mask & ~_erode(mask, 1)
    img[edge] = colour


def save_jpeg(path: Path, img: np.ndarray, quality: int = 85) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(path, quality=quality)


def pair_image(r: dict, mode: str, outline_regions=()) -> np.ndarray:
    """Ours | Forza side by side, flagged regions outlined on both."""
    a = to_srgb8(r["ours"][mode], r["alpha"]["ours"])
    b = to_srgb8(r["ref"][mode], r["alpha"]["ref"])
    for name in outline_regions:
        outline(a, r["regions"]["ours"] == RID[name])
        outline(b, r["regions"]["ref"] == RID[name])
    gap = np.full((a.shape[0], 4, 3), 20, np.uint8)
    return np.concatenate([a, gap, b], axis=1)


# --- Forza's own pictures: a check of the reference renderer -----------------------------------

THUMB_CAMERA = cr.Camera(-62.0, 6.0, 1.45)  # FM4's thumbnails: front left, near eye level


def forza_thumbnail(media: str, game: str | None) -> np.ndarray | None:
    """Forza's own picture of the car (RGBA): FM4's Thumbnails/Thumbnail_<id>.xds by its
    gamedb id (out/db/fm4_gamedb.slt), else FH1's (gui_assets.forza_car_id); the car part
    (left two thirds; the right third is a top view)."""
    import sqlite3

    from dsfforza import REPO
    from dsfforza.game import gui_assets as g

    ui = g.ForzaUI.of(None)
    found = None
    db = REPO / "out" / "db" / "fm4_gamedb.slt"
    if db.exists():
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT Id FROM Data_Car WHERE lower(MediaName) = lower(?)", (media,)).fetchone()
        finally:
            con.close()
        if row:
            found = ui._read_fallback(f"Thumbnails/Thumbnail_{row[0]}.xds")
    if found is None:
        cid = g.forza_car_id(media)
        found = ui.thumbnail(cid) if cid else None
    if found is None:
        return None
    w, h, px = found
    img = np.frombuffer(bytes(px), np.uint8).reshape(h, w, 4)
    return img[:, : w * 2 // 3][:, ::-1]  # mirrored, like FH1's (gui_assets.forza_car_image)


def thumbnail_check(build: Path, media: str, out: Path, size=(640, 300)) -> Path | None:
    """Forza's picture | the reference | ours, at a camera like the picture's and in the
    picture's paint (the median of its saturated car pixels): out/thumb-<media>.jpg."""
    from dsfforza.game.gui_assets import _root_entries

    report = car_report(build, media)
    if report is None:
        return None
    thumb = forza_thumbnail(media, report.get("game"))
    if thumb is None:
        return None
    rgb = thumb[..., :3].astype(np.float32)
    sat = rgb.max(axis=-1) - rgb.min(axis=-1)
    car = (thumb[..., 3] > 128) & (rgb.max(axis=-1) > 20)
    colour = np.median(rgb[car & (sat > 40)], axis=0) if (car & (sat > 40)).sum() > 200 else np.array([40, 40, 40])
    archive = Path(build) / "dngvehicles.sp"
    with archive.open("rb") as stream:
        roots = _root_entries(archive, stream)
    pair = build_pair(archive, roots, report["entry"]["spool_id"], media, report)
    paint = cr.Paint.flat(tuple(int(c) for c in colour))
    for scene in (pair.ours, pair.ref):
        for m in scene.materials:
            if m.paint is not None:
                m.paint = (
                    paint
                    if m.kind in cr.PAINT_KINDS
                    else cr.Paint(
                        tuple(c * fr.PAINT_TEXEL for c in paint.c2), tuple(c * fr.PAINT_TEXEL for c in paint.c3)
                    )
                )
    view = View("thumb", THUMB_CAMERA, modes=("day",))
    r = render_view(pair, view, size)
    from PIL import Image

    t = Image.fromarray(thumb[..., :3]).resize((int(thumb.shape[1] * size[1] / thumb.shape[0]), size[1]))
    parts = [
        np.asarray(t),
        to_srgb8(r["ref"]["day"], r["alpha"]["ref"]),
        to_srgb8(r["ours"]["day"], r["alpha"]["ours"]),
    ]
    gap = np.full((size[1], 4, 3), 20, np.uint8)
    path = Path(out) / f"thumb-{media}.jpg"
    save_jpeg(path, np.concatenate([parts[0], gap, parts[1], gap, parts[2]], axis=1))
    return path


# --- the run -----------------------------------------------------------------------------------


def score_view(r: dict, modes) -> dict:
    """{"day": region scores, "glow_<mode>": glow scores} of one rendered view."""
    mm = {"day": region_scores(r, "day")}
    for mode in modes:
        if mode != "day":
            mm[f"glow_{mode}"] = glow_scores(r, mode)
    return mm


HDR_SCALE = 4096  # --keep: linear colour stored as uint16 x this (16 times the exposure's white)


def save_renders(path: Path, r: dict) -> None:
    """A view's renders and ID maps (render_view's output), for `rescore`."""
    arrays = {
        "regions_ours": r["regions"]["ours"],
        "regions_ref": r["regions"]["ref"],
        "programs": r["programs"],
        "alpha_ours": (np.clip(r["alpha"]["ours"], 0, 1) * 255).astype(np.uint8),
        "alpha_ref": (np.clip(r["alpha"]["ref"], 0, 1) * 255).astype(np.uint8),
    }
    for side in ("ours", "ref"):
        for mode, c in r[side].items():
            arrays[f"{side}_{mode}"] = np.clip(c * HDR_SCALE, 0, 65535).astype(np.uint16)
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **arrays)


def load_renders(path: Path) -> dict:
    z = np.load(path)
    r = {
        "regions": {"ours": z["regions_ours"], "ref": z["regions_ref"]},
        "programs": z["programs"],
        "alpha": {"ours": z["alpha_ours"] / 255.0, "ref": z["alpha_ref"] / 255.0},
        "ours": {},
        "ref": {},
    }
    for key in z.files:
        side, _, mode = key.partition("_")
        if side in ("ours", "ref") and (mode in cr.LIGHTINGS or mode == "dark_base"):
            r[side][mode] = z[key].astype(np.float32) / HDR_SCALE
    return r


def _save_images(folder: Path, images: dict, flags: list) -> None:
    for key, r in images.items():
        regions = sorted({f["region"] for f in flags if f["view"] == key and f["region"] in REGIONS})
        for mode in r["ours"]:
            if mode in cr.LIGHTINGS:
                save_jpeg(folder / f"{key}-{mode}.jpg", pair_image(r, mode, regions))


def compare_car(
    archive: Path, roots, car: dict, out: Path, views=DEFAULT_VIEWS, size=SIZE, entries=("exterior",), keep=False
) -> dict:
    """Render, score and save one car: out/cars/<media>.json and its images (with keep,
    each view's renders too: out/cars/<media>/<view>.npz, for rescore)."""
    media, sid = car["media"], car["spool_id"]
    t0 = time.time()
    report = car_report(archive.parent, media)
    result: dict = {"media": media, "spool_id": sid, "game": car.get("game")}
    if report is None or "placement" not in report.get("entry", {}):
        result["error"] = "no converter report (work/<media>.json) in the build"
        return result
    folder = out / "cars" / media
    try:
        metrics: dict = {}
        images: dict = {}
        for entry in entries:
            if entry == "interior":
                camera = cockpit_camera(report)
                if camera is None:
                    result.setdefault("skipped", []).append("interior: no cockpit in the report")
                    continue
                pair = build_cockpit_pair(archive, roots, sid, media, report)
                result.setdefault("match", {})[entry] = pair.match
                view = View("cockpit", camera, modes=("day", "dark"))
                r = render_view(pair, view, size)
                metrics["cockpit"] = {"day": region_scores(r, "day")}
                images["cockpit"] = r
                continue
            pair = build_pair(archive, roots, sid, media, report, entry)
            result.setdefault("match", {})[entry] = pair.match
            for vname in views:
                if entry == "full" and vname not in ("front3q", "rear3q"):
                    continue
                view = VIEWS[vname]
                if entry == "full":
                    view = View(view.name, view.camera, view.zoom, ("day", "dark"))
                r = render_view(pair, view, size)
                key = vname if entry == "exterior" else f"{entry}-{vname}"
                metrics[key] = score_view(r, view.modes)
                images[key] = r
        flags = flags_for(metrics)
        result["metrics"] = metrics
        result["flags"] = flags
        result["score"] = car_score(flags)
        _save_images(folder, images, flags)
        if keep:
            for key, r in images.items():
                save_renders(folder / f"{key}.npz", r)
        result["seconds"] = round(time.time() - t0, 1)
    except Exception as error:  # one car's failure is a result, not the run's end
        import traceback

        result["error"] = f"{type(error).__name__}: {error}"
        result["traceback"] = traceback.format_exc(limit=6)
    (out / "cars").mkdir(parents=True, exist_ok=True)
    (out / "cars" / f"{media}.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result


RESULT_KEYS = ("media", "spool_id", "game", "score", "flags", "error", "seconds", "match")


def _scoring_stamp() -> str:
    """Digest of this module's source: a car rescored with the same code is not done again."""
    import hashlib

    return hashlib.blake2b(Path(__file__).read_bytes(), digest_size=8).hexdigest()


def _rescore_car(path: str, stamp: str) -> dict:
    path = Path(path)
    res = json.loads(path.read_text(encoding="utf-8"))
    folder = path.parent / res["media"]
    stored = sorted(folder.glob("*.npz")) if folder.exists() else []
    if stored and res.get("rescored") != stamp:
        images = {p.stem: load_renders(p) for p in stored}
        metrics = {}
        for key, r in images.items():
            modes = [m for m in r["ours"] if m in cr.LIGHTINGS]
            metrics[key] = {"day": region_scores(r, "day")} if key == "cockpit" else score_view(r, modes)
        flags = flags_for(metrics)
        res.update({"metrics": metrics, "flags": flags, "score": car_score(flags), "rescored": stamp})
        _save_images(folder, images, flags)
        path.write_text(json.dumps(res, indent=1), encoding="utf-8")
    return {k: res.get(k) for k in RESULT_KEYS}


def _rescore_worker(args):
    idle_priority()
    paths, stamp = args
    return [_rescore_car(p, stamp) for p in paths]


def rescore(run_dir: Path, jobs: int = 1) -> list[dict]:
    """Score a --keep run again from its stored renders (after a change of the metrics or
    the limits); rewrites the cars' JSON and images and roster.json. Cars already rescored
    by this code (their "rescored" stamp) are kept, so an interrupted rescore resumes."""
    run_dir = Path(run_dir)
    paths = [str(p) for p in sorted((run_dir / "cars").glob("*.json"))]
    stamp = _scoring_stamp()
    jobs = max(1, min(jobs, len(paths) or 1))
    if jobs == 1:
        results = _rescore_worker((paths, stamp))
    else:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(jobs) as pool:
            parts = pool.map(_rescore_worker, [(paths[i::jobs], stamp) for i in range(jobs)])
            results = [r for part in parts for r in part]
    roster_path = run_dir / "roster.json"
    summary = json.loads(roster_path.read_text(encoding="utf-8")) if roster_path.exists() else {"cars": len(results)}
    summary["ranking"] = sorted(results, key=lambda r: -(r.get("score") or 0))
    summary["errors"] = sum(1 for r in results if r.get("error"))
    summary.setdefault("seconds", 0)
    summary.setdefault("views", [])
    summary.setdefault("build", str(run_dir))
    roster_path.write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary["ranking"]


def _worker(args):
    archive, cars, out, views, size, entries, keep = args
    from dsfforza.game.gui_assets import _root_entries

    archive = Path(archive)
    with archive.open("rb") as stream:
        roots = _root_entries(archive, stream)
    results = []
    for car in cars:
        r = compare_car(archive, roots, car, Path(out), views, size, entries, keep)
        print(f"{car['media']}: score {r.get('score')} {r.get('error', '')} ({r.get('seconds')} s)", flush=True)
        results.append(
            {k: r.get(k) for k in ("media", "spool_id", "game", "score", "flags", "error", "seconds", "match")}
        )
    return results


def run(
    build: Path,
    out: Path,
    cars: list[dict],
    jobs: int = 1,
    views=DEFAULT_VIEWS,
    size=SIZE,
    entries=("exterior",),
    keep: bool = False,
) -> list[dict]:
    archive = Path(build) / "dngvehicles.sp"
    out.mkdir(parents=True, exist_ok=True)
    jobs = max(1, min(jobs, len(cars)))
    chunks = [cars[i::jobs] for i in range(jobs)]
    t0 = time.time()
    if jobs == 1:
        results = _worker((str(archive), cars, str(out), views, size, entries, keep))
    else:
        from concurrent.futures import ProcessPoolExecutor

        os.environ.setdefault("OMP_NUM_THREADS", "1")
        with ProcessPoolExecutor(jobs) as pool:
            results = [
                r
                for part in pool.map(_worker, [(str(archive), c, str(out), views, size, entries, keep) for c in chunks])
                for r in part
            ]
    # Every car --out has (a --resume run's earlier ones too).
    keys = ("media", "spool_id", "game", "score", "flags", "error", "seconds", "match")
    done = {r["media"]: r for r in results}
    for path in (out / "cars").glob("*.json"):
        if path.stem not in done:
            try:
                r = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            done[path.stem] = {k: r.get(k) for k in keys}
    results = list(done.values())
    ranking = sorted(results, key=lambda r: -(r.get("score") or 0))
    summary = {
        "build": str(build),
        "cars": len(results),
        "errors": sum(1 for r in results if r.get("error")),
        "seconds": round(time.time() - t0, 1),
        "views": list(views),
        "entries": list(entries),
        "size": list(size),
        "ranking": ranking,
    }
    (out / "roster.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return ranking


def idle_priority() -> None:
    """Run at Idle priority (Windows; nice 19 elsewhere): the renders are background work
    beside builds and VM runs, and the worker processes inherit it."""
    import ctypes

    try:
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.SetPriorityClass.argtypes = (wintypes.HANDLE, wintypes.DWORD)
            kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x00000040)  # IDLE_PRIORITY_CLASS
        else:
            os.nice(19)
    except (OSError, AttributeError):
        pass


def _done(out: Path, media: str) -> bool:
    path = out / "cars" / f"{media}.json"
    if not path.exists():
        return False
    try:
        r = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return "metrics" in r and not r.get("error")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dsff render_compare", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="render and score a build's cars against Forza")
    p.add_argument("--build", type=Path, required=True, help="build folder (dngvehicles.sp, batch-report.json, work/)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cars", help="media names or spool ids, comma-separated (default: every car)")
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--views", default=",".join(DEFAULT_VIEWS))
    p.add_argument("--size", default=f"{SIZE[0]}x{SIZE[1]}")
    p.add_argument("--entries", default="exterior", help="exterior,full")
    p.add_argument("--no-sheet", action="store_true")
    p.add_argument("--resume", action="store_true", help="skip the cars --out already has scores for")
    p.add_argument(
        "--keep", action="store_true", help="store each view's renders (cars/<media>/<view>.npz) for rescore"
    )
    q = sub.add_parser("rescore", help="score a --keep run again from its stored renders")
    q.add_argument("run", type=Path)
    q.add_argument("--jobs", type=int, default=1)
    d = sub.add_parser("diff", help="before/after: the cars two builds draw differently, and how")
    d.add_argument("--before", type=Path, required=True, help="build folder (master's)")
    d.add_argument("--after", type=Path, required=True, help="build folder (the change's)")
    d.add_argument("--out", type=Path, required=True)
    d.add_argument("--jobs", type=int, default=1)
    d.add_argument("--views", default=",".join(DEFAULT_VIEWS))
    d.add_argument("--size", default=f"{SIZE[0]}x{SIZE[1]}")
    d.add_argument("--limit", type=int, default=0, help="render at most this many changed cars")
    t = sub.add_parser("thumbnail", help="Forza's own picture | the reference | ours (a check of the reference)")
    t.add_argument("--build", type=Path, required=True)
    t.add_argument("--cars", required=True)
    t.add_argument("--out", type=Path, required=True)
    s = sub.add_parser("sheet", help="write the HTML contact sheet of a run")
    s.add_argument("run", type=Path)
    s.add_argument("--top", type=int, default=0, help="only the N worst cars")
    args = ap.parse_args(argv)
    idle_priority()
    if args.cmd == "run":
        cars = roster(args.build)
        if args.cars:
            want = [w.strip() for w in args.cars.split(",") if w.strip()]
            cars = [c for c in cars if c["media"] in want or str(c["spool_id"]) in want]
        if args.resume:
            cars = [c for c in cars if not _done(args.out, c["media"])]
        w, h = (int(x) for x in args.size.lower().split("x"))
        ranking = run(
            args.build,
            args.out,
            cars,
            args.jobs,
            tuple(args.views.split(",")),
            (w, h),
            tuple(args.entries.split(",")),
            args.keep,
        )
        for r in ranking[:20]:
            print(
                f"{r.get('score', 0):7.2f}  {r['media']:32} " + "; ".join(f["text"] for f in (r.get("flags") or [])[:3])
            )
        if not args.no_sheet:
            from dsfforza.harness import render_sheet

            print(render_sheet.write(args.out))
        return 0
    if args.cmd == "diff":
        from dsfforza.harness import render_diff

        w, h = (int(x) for x in args.size.lower().split("x"))
        s = render_diff.run(
            args.before, args.after, args.out, args.jobs, tuple(args.views.split(",")), (w, h), args.limit
        )
        print(
            f"{s['changed']} changed ({s['drawn_differently']} drawn differently, {len(s['renders_alike'])} alike), "
            f"{s['unchanged']} unchanged ({s['seconds']} s)"
        )
        for car in s["cars"][:30]:
            top = "; ".join(
                f"{c['region']} {c['view']} {c['mode']} {c['changed']:.0%}" for c in (car.get("changes") or [])[:3]
            )
            print(f"  {car['media']:32} {car.get('score_before')} -> {car.get('score_after')}  {top}")
        print(render_diff.write_sheet(args.out))
        return 0
    if args.cmd == "rescore":
        for r in rescore(args.run, args.jobs)[:20]:
            print(
                f"{r.get('score') or 0:7.2f}  {r['media']:32} "
                + "; ".join(f["text"] for f in (r.get("flags") or [])[:3])
            )
        return 0
    if args.cmd == "thumbnail":
        for media in args.cars.split(","):
            print(media, thumbnail_check(args.build, media.strip(), args.out))
        return 0
    if args.cmd == "sheet":
        from dsfforza.harness import render_sheet

        print(render_sheet.write(args.run, top=args.top))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
