"""Forza lamp clusters drawn with Forza's own shaders, against Forza's pictures (research/forza-lenses-re.md).

The car's main carbin is rasterised by render_compare's renderer (forza_render.body_scene,
car_render.rasterize: per pixel the nearest opaque triangle and up to three see-through
layers). Every pixel whose opaque triangle or a layer is a lamp, lens or lamp-glass
technique is then shaded by running that technique's race pixel shaders from
shaders_v16.fxobj in the Xenos emulator (forza_lens.Forza.run) on the pixel's own texel,
normal and view, and the layers are blended far to near with each pass's blend in the
shaders' sqrt-encoded space. The rest of the car keeps car_render's shading. car_render's
studio lights stand in for Forza's (key light as the sun, its sky ambient as the SH term,
its environment as the cube map).

    uv run python tools/re/forza_cluster.py render FER_F50_95 --camera thumb --out out/re3/cl
    uv run python tools/re/forza_cluster.py compare FER_F50_95 VOL_S60_11 ... --out out/re3/cl

`render` writes <car>-<camera>-<state>.png: this model | render_compare's reference model.
`compare` puts Forza's own thumbnail beside them and scores the lamp region (structure
correlation and chroma against the thumbnail, after a per-image exposure fit).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

from dsfforza import REPO
from dsfforza.game import car_render as cr
from dsfforza.harness import forza_render as fr

sys.path.insert(0, str(Path(__file__).parent))
import forza_lens as fl

THUMB = cr.Camera(-62.0, 6.0, 1.45)
CAMERAS = {
    "thumb": THUMB,  # render_compare.THUMB_CAMERA: FM4's thumbnails
    "front": cr.Camera(0.0, 4.0, 1.3),
    "rear": cr.Camera(180.0, 4.0, 1.3),
    "rear34": cr.Camera(150.0, 8.0, 1.2),
}
STATES = {
    "off": {},
    # FH1: dynamicLightsAmount (brake, reverse, indicator L, R); FM4: brakeReverseAmount (brake, reverse)
    "brake": {"dynamicLightsAmount": [1.0, 0.0, 0.0, 0.0], "brakeReverseAmount": [1.0, 0.0, 0.0, 0.0]},
    "reverse": {"dynamicLightsAmount": [0.0, 1.0, 0.0, 0.0], "brakeReverseAmount": [0.0, 1.0, 0.0, 0.0]},
    "running": {"dynamicLights2": [1.0, 0.0, 1.0, 0.0]},
}


# The blend of passes that set none (glass, window, lights_glass, lights_gls_*, the lamps): the
# game's transparent bucket, not in the effect. "alpha" (SRCALPHA, INVSRCALPHA) is the
# assumption; "add" (SRCALPHA, ONE) is the alternative the R8 race car's thumbnail suggests.
BUCKET_BLENDS = {"alpha": (6, 7), "add": (6, 1)}


def car_folder(media: str) -> Path | None:
    for g in ("fh1", "fm4"):
        p = REPO / "out" / "cache" / g / media
        if p.is_dir():
            return p
    return None


def is_forza_lamp(tech: str) -> bool:
    return bool(fl.LENS_RE.match(tech.lower())) and not tech.lower().startswith(("window", "black_glass"))


def render(
    media: str,
    camera: cr.Camera,
    size=(512, 288),
    state: str = "off",
    reflection: float = 1.0,
    game: str | None = None,
    sun_scale: float = 1.0,
    bucket: str = "alpha",
):
    """(spec, reference, lamp mask, alpha): linear RGB (h, w, 3) of this model and of
    render_compare's reference, the lamp pixels, coverage."""
    folder = car_folder(media)
    if folder is None:
        raise FileNotFoundError(media)
    game = game or folder.parent.name
    paint = cr.Paint.flat((120, 20, 20))
    fs = fr.body_scene(folder, media, {"dy": 0.0, "dz": 0.0}, 1, paint)
    scene = fs.scene
    w, h = size
    view = cr._View(scene, camera, w, h)
    frame = cr.rasterize(scene, view, layers=3)
    ref_colour, alpha = cr.shade(scene, frame)
    fz = fl.Forza.load(game)
    fz.bucket_blend = BUCKET_BLENDS[bucket]
    car_settings = None
    if (folder / "ShaderSettings.xml").exists():
        car_settings = fl.parse_settings((folder / "ShaderSettings.xml").read_bytes().decode("latin-1"))
    tech_of = np.array([fs.origins[t].technique for t in scene.tag]) if scene.tag is not None else None
    front_of = np.array([fs.origins[t].region == "front_lamps" for t in scene.tag])
    light = cr._light(view)
    lamp_state = STATES[state]

    def shade_layer(k: np.ndarray, tri: np.ndarray):
        """Per pixel: the technique's pass results (list of PassResult) or None."""
        n, uv, _ao, ray = cr._surface(scene, view, frame.sx, frame.sy, frame.sw, k, tri)
        out = []
        for i in range(len(k)):
            tech = tech_of[tri[i]].lower()
            if not is_forza_lamp(tech) or tech not in fz.techs:
                out.append(None)
                continue
            mat = scene.materials[scene.mat[tri[i]]]
            if mat.texture is not None:
                texel = tuple(float(x) for x in cr._sample(mat.texture, uv[i : i + 1])[0])
            else:
                texel = (0.0, 0.0, 0.0, 0.0)
            nn = n[i]
            eye = -ray[i]
            refl = ray[i] - 2 * float(ray[i] @ nn) * nn
            env_lin = cr._environment(refl[None])[0]
            sky = (cr.GROUND_AMBIENT + (cr.SKY_AMBIENT - cr.GROUND_AMBIENT) * (1 + nn[1]) / 2) * np.ones(3)
            sc = fl.Scene(
                normal=tuple(nn),
                eye=tuple(eye),
                sun=tuple(light),
                sun_colour=(cr.KEY_POWER * sun_scale,) * 3,
                sky=tuple(sky),
                env=tuple(np.sqrt(np.clip(env_lin, 0, None))),
                reflection=reflection,
            )
            out.append(fz.run(tech, texel, dict(lamp_state), sc, car=car_settings))
        return out

    size_px = w * h
    enc = np.sqrt(np.clip(ref_colour, 0, None)).astype(np.float64)  # the reference as a start
    lamp = np.zeros(size_px, bool)
    front = np.zeros(size_px, bool)
    hit = np.flatnonzero(frame.tri >= 0)
    front[hit] = front_of[frame.tri[hit]]
    for on, gtri in frame.layers:
        front[on] |= front_of[gtri]
    if len(hit):
        res = shade_layer(hit, frame.tri[hit])
        for i, r in enumerate(res):
            if r is not None:
                enc[hit[i]] = fl.composite((0.0, 0.0, 0.0), r, clamp=1e9)
                lamp[hit[i]] = True
    # Layers far to near: Forza's passes over what is behind, in the encoded space.
    for on, gtri in reversed(frame.layers):
        res = shade_layer(on, gtri)
        for i, r in enumerate(res):
            if r is None:
                continue
            p = on[i]
            enc[p] = fl.composite(tuple(enc[p]), r, clamp=1e9)
            lamp[p] = True
    spec = (enc**2).reshape(h, w, 3)
    ids = np.full(size_px, -1, np.int64)
    ids[hit] = scene.tag[frame.tri[hit]]
    for on, gtri in reversed(frame.layers):
        ids[on] = scene.tag[gtri]
    render.ids = ids.reshape(h, w)
    render.front = (front & lamp).reshape(h, w)
    return spec, ref_colour.reshape(h, w, 3), lamp.reshape(h, w), alpha.reshape(h, w)


def to_srgb(lin: np.ndarray, exposure: float = cr.EXPOSURE) -> np.ndarray:
    return (np.clip(cr._linear_to_srgb(np.clip(lin * exposure, 0, None)), 0, 1) * 255 + 0.5).astype(np.uint8)


def thumbnail(media: str) -> np.ndarray | None:
    from dsfforza.harness import render_compare as rc

    t = rc.forza_thumbnail(media, None)
    return None if t is None else t[..., :3]


def _box(a: np.ndarray, r: int) -> np.ndarray:
    """Mean over a (2r+1)^2 box, edges clamped."""
    p = np.pad(a, r, mode="edge")
    c = np.cumsum(np.cumsum(np.pad(p, ((1, 0), (1, 0))), axis=0), axis=1)
    k = 2 * r + 1
    return (c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]) / (k * k)


def _dilate(m: np.ndarray, it: int = 1) -> np.ndarray:
    for _ in range(it):
        p = np.pad(m, 1)
        m = p[1:-1, 1:-1] | p[:-2, 1:-1] | p[2:, 1:-1] | p[1:-1, :-2] | p[1:-1, 2:]
    return m


def _luma(img: np.ndarray) -> np.ndarray:
    f = img.astype(np.float64) / 255
    return 0.2126 * f[..., 0] + 0.7152 * f[..., 1] + 0.0722 * f[..., 2]


def align(thumb: np.ndarray, alpha: np.ndarray) -> tuple[np.ndarray, tuple]:
    """Thumbnail resampled onto the render's grid by the box of their car silhouettes."""
    from PIL import Image

    tm = _luma(thumb) > 0.03
    ys, xs = np.nonzero(tm)
    ry, rx = np.nonzero(alpha > 0.5)
    if not len(xs) or not len(rx):
        return np.zeros(alpha.shape + (3,), np.uint8), ()
    tb = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
    rb = (rx.min(), ry.min(), rx.max() + 1, ry.max() + 1)
    crop = Image.fromarray(thumb).crop(tb).resize((rb[2] - rb[0], rb[3] - rb[1]), Image.BILINEAR)
    out = np.zeros(alpha.shape + (3,), np.uint8)
    out[rb[1] : rb[3], rb[0] : rb[2]] = np.asarray(crop)
    return out, rb


# Head lamp boxes (x0, y0, x1, y1) in the thumbnails (512 x 288, the car part mirrored as
# render_compare.forza_thumbnail returns it), picked by eye: the nearer head lamp.
THUMB_BOXES = {
    "FER_F50_95": (125, 180, 175, 197),
    "VOL_S60_11": (85, 155, 165, 190),
    "FOR_MustangGT_05": (30, 160, 125, 192),
    "AUD_R8LMS_09": (55, 195, 165, 225),
    "AUD_2_R8_06": (100, 190, 160, 240),
    "LOT_Carlton_89": (20, 160, 100, 190),
    "VW_GTIVR6_98": (75, 160, 130, 185),
}


def stats(img: np.ndarray, mask: np.ndarray) -> dict:
    """What tells a milky plate from a lamp seen through clear glass, independent of exposure
    and registration: local contrast over mean luma, the p90 / p10 luma spread, saturation
    and chroma (r and g shares) over the region."""
    lum = _luma(img)
    loc = lum - _box(lum, 2)
    v = lum[mask]
    f = img[mask].astype(np.float64) / 255 + 1e-3
    mx, mn = f.max(axis=1), f.min(axis=1)
    share = f / f.sum(axis=1, keepdims=True)
    return {
        "contrast": round(float(loc[mask].std() / max(v.mean(), 1e-3)), 3),
        "spread": round(float(np.percentile(v, 90) / max(np.percentile(v, 10), 0.01)), 2),
        "saturation": round(float(((mx - mn) / mx).mean()), 3),
        "chroma": [round(float(share[:, 0].mean()), 3), round(float(share[:, 1].mean()), 3)],
    }


def fit_exposure(lin: np.ndarray, thumb, _unused=None, target: float | None = None, mask=None) -> float:
    """The exposure that brings the image's mean luma over `mask` to `target`."""
    lo, hi = 0.001, 1000.0
    for _ in range(50):
        mid = math.sqrt(lo * hi)
        if _luma(to_srgb(lin, mid))[mask].mean() < target:
            lo = mid
        else:
            hi = mid
    return math.sqrt(lo * hi)


def main(argv: list[str] | None = None) -> None:
    from PIL import Image

    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("render")
    r.add_argument("cars", nargs="+")
    r.add_argument("--camera", default="rear34", choices=sorted(CAMERAS))
    r.add_argument("--state", default="off", choices=sorted(STATES))
    r.add_argument("--reflection", type=float, default=1.0, help="reflectionScaler (a game constant)")
    r.add_argument("--size", default="768,432")
    r.add_argument("--out", type=Path, default=fl.DATA / "cluster")
    c = sub.add_parser("compare")
    c.add_argument("cars", nargs="+")
    c.add_argument("--reflection", type=float, default=1.0)
    c.add_argument("--bucket", default="alpha", choices=sorted(BUCKET_BLENDS))
    c.add_argument("--out", type=Path, default=fl.DATA / "cluster")
    args = ap.parse_args(argv)
    fl._idle()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "render":
        size = tuple(int(x) for x in args.size.split(","))
        for media in args.cars:
            spec, ref, lamp, _alpha = render(media, CAMERAS[args.camera], size, args.state, args.reflection)
            img = np.concatenate([to_srgb(spec), np.full((size[1], 4, 3), 30, np.uint8), to_srgb(ref)], axis=1)
            path = args.out / f"{media}-{args.camera}-{args.state}.png"
            Image.fromarray(img).save(path)
            ys, xs = np.nonzero(lamp)
            if len(xs):
                pad = 10
                box = (
                    max(xs.min() - pad, 0),
                    max(ys.min() - pad, 0),
                    min(xs.max() + pad, size[0]),
                    min(ys.max() + pad, size[1]),
                )
                a, b = (Image.fromarray(to_srgb(x)).crop(box) for x in (spec, ref))
                k = max(1, 360 // max(a.height, 1))
                a, b = (i.resize((i.width * k, i.height * k), Image.NEAREST) for i in (a, b))
                sheet = Image.new("RGB", (a.width * 2 + 6, a.height), (30, 30, 30))
                sheet.paste(a, (0, 0))
                sheet.paste(b, (a.width + 6, 0))
                sheet.save(path.with_name(path.stem + "-crop.png"))
            print(path, "lamp pixels", int(lamp.sum()))
        return
    results = {}
    for media in args.cars:
        thumb = thumbnail(media)
        if thumb is None:
            print(media, "no thumbnail")
            continue
        size = (thumb.shape[1], thumb.shape[0])
        spec, ref, lamp, _alpha = render(media, THUMB, size, "off", args.reflection, bucket=args.bucket)
        front = render.front
        ys, xs = np.nonzero(front)
        if len(xs) < 30 or media not in THUMB_BOXES:
            print(media, "no front lamp in view or no thumbnail box")
            continue
        # the nearer head lamp: the largest group of front-lamp pixels by column gaps
        cols = np.unique(xs)
        groups = np.split(cols, np.flatnonzero(np.diff(cols) > 6) + 1)
        keep = max(groups, key=lambda g: front[:, g].sum())
        front = front & np.isin(np.arange(size[0])[None, :], keep)
        ys, xs = np.nonzero(front)
        pad = 4
        x0, y0, x1, y1 = (
            max(xs.min() - pad, 0),
            max(ys.min() - pad, 0),
            min(xs.max() + pad, size[0]),
            min(ys.max() + pad, size[1]),
        )
        tx0, ty0, tx1, ty1 = THUMB_BOXES[media]
        t_patch = thumb[ty0:ty1, tx0:tx1]
        t_mask = _luma(t_patch) > 0.02
        row = {"thumbnail": stats(t_patch, t_mask)}
        imgs = [t_patch]
        mask = front[y0:y1, x0:x1]
        for name, lin in (("spec", spec), ("reference", ref)):
            patch = lin[y0:y1, x0:x1]
            e = fit_exposure(patch, t_patch, None, target=float(_luma(t_patch)[t_mask].mean()), mask=mask)
            im = to_srgb(patch, e)
            imgs.append(im)
            row[name] = stats(im, mask) | {"exposure": round(e, 3)}
        results[media] = row
        hgt = 160
        crops = [
            Image.fromarray(i).resize((max(1, round(i.shape[1] * hgt / i.shape[0])), hgt), Image.NEAREST) for i in imgs
        ]
        sheet = Image.new("RGB", (sum(ci.width for ci in crops) + 8 * len(crops), hgt), (30, 30, 30))
        x = 0
        for ci in crops:
            sheet.paste(ci, (x, 0))
            x += ci.width + 8
        sheet.save(args.out / f"{media}-thumb-compare.png")
        print(media, json.dumps(row))
    (args.out / "compare.json").write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
