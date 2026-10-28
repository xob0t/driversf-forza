"""Garage pictures: the numpy image helpers of gui_assets give what their per-pixel
loops gave, and car_render's rasteriser, camera and glass behave (synthetic scenes,
no game files).

uv run python -m tests garage
"""

from __future__ import annotations

import math
import random

import numpy as np

from dsfforza.game import car_render as cr
from dsfforza.game import gui_assets as g

# --- gui_assets.Image: the original loops ------------------------------------------------


def _resize_reference(img: g.Image, width: int, height: int) -> g.Image:
    out = g.Image(width, height)
    sx, sy = img.w / width, img.h / height
    for y in range(height):
        fy0, fy1 = y * sy, (y + 1) * sy
        for x in range(width):
            fx0, fx1 = x * sx, (x + 1) * sx
            acc, total = [0.0, 0.0, 0.0, 0.0], 0.0
            for yy in range(int(fy0), min(img.h, int(math.ceil(fy1)))):
                wy = min(fy1, yy + 1) - max(fy0, yy)
                for xx in range(int(fx0), min(img.w, int(math.ceil(fx1)))):
                    wgt = wy * (min(fx1, xx + 1) - max(fx0, xx))
                    o = (yy * img.w + xx) * 4
                    a = img.px[o + 3] * wgt
                    acc[0] += img.px[o] * a
                    acc[1] += img.px[o + 1] * a
                    acc[2] += img.px[o + 2] * a
                    acc[3] += a
                    total += wgt
            o = (y * width + x) * 4
            if acc[3] > 0:
                out.px[o : o + 3] = bytes(min(255, int(c / acc[3] + 0.5)) for c in acc[:3])
            out.px[o + 3] = min(255, int(acc[3] / max(total, 1e-9) + 0.5))
    return out


def _paste_reference(img: g.Image, other: g.Image, left: int, top: int) -> None:
    for y in range(other.h):
        ty = top + y
        if not 0 <= ty < img.h:
            continue
        for x in range(other.w):
            tx = left + x
            if not 0 <= tx < img.w:
                continue
            o = (y * other.w + x) * 4
            a = other.px[o + 3] / 255
            if a <= 0:
                continue
            q = (ty * img.w + tx) * 4
            b = img.px[q + 3] / 255
            out_a = a + b * (1 - a)
            for c in range(3):
                img.px[q + c] = int((other.px[o + c] * a + img.px[q + c] * b * (1 - a)) / out_a + 0.5)
            img.px[q + 3] = int(out_a * 255 + 0.5)


def _random_image(rng: random.Random, w: int, h: int) -> g.Image:
    px = bytearray(rng.getrandbits(8) for _ in range(w * h * 4))
    for i in range(3, len(px), 4):  # some fully transparent and fully opaque pixels
        px[i] = rng.choice((0, 255, px[i]))
    return g.Image(w, h, px)


def test_image_resize_matches_the_loop():
    rng = random.Random(7)
    for (w, h), (ow, oh) in (((37, 23), (11, 9)), ((64, 40), (30, 17)), ((20, 12), (20, 12)), ((9, 31), (4, 13))):
        img = _random_image(rng, w, h)
        new, old = img.resize(ow, oh).array().astype(int), _resize_reference(img, ow, oh).array().astype(int)
        # The same sums in another order: at most one step where a value sits on .5.
        assert np.abs(new - old).max() <= 1
        assert (new != old).mean() < 0.01


def test_image_paste_matches_the_loop():
    rng = random.Random(8)
    for left, top in ((3, 2), (-4, -3), (10, 6), (30, 1)):
        base, top_img = _random_image(rng, 24, 16), _random_image(rng, 13, 11)
        want = g.Image(base.w, base.h, base.px)
        _paste_reference(want, top_img, left, top)
        base.paste(top_img, left, top)
        assert base.px == want.px


def test_image_bbox_crop_flip():
    rng = random.Random(9)
    img = g.Image(30, 20)
    img.array()[5:12, 7:19] = [1, 2, 3, 200]
    assert img.bbox(8) == (7, 5, 19, 12)
    assert img.bbox(8, (10, 0, 30, 20)) == (10, 5, 19, 12)
    assert g.Image(4, 4).bbox() is None
    img = _random_image(rng, 9, 5)
    flipped = img.flip_h()
    for y in range(5):
        for x in range(9):
            o, q = (y * 9 + x) * 4, (y * 9 + 8 - x) * 4
            assert flipped.px[q : q + 4] == img.px[o : o + 4]
    crop = img.crop(2, 1, 7, 4)
    assert (crop.w, crop.h) == (5, 3)
    assert crop.px[:4] == img.px[(1 * 9 + 2) * 4 : (1 * 9 + 2) * 4 + 4]


# --- car_render --------------------------------------------------------------------------


def test_fragments_are_the_covered_pixel_centres():
    rng = np.random.default_rng(3)
    width, height = 40, 30
    sx = rng.uniform(-5, 45, (60, 3))
    sy = rng.uniform(-5, 35, (60, 3))
    sx[:20] = sx[:20, :1] + rng.uniform(-3, 3, (20, 3))  # small triangles too
    sy[:20] = sy[:20, :1] + rng.uniform(-3, 3, (20, 3))
    sw = rng.uniform(0.1, 1.0, (60, 3))
    k, w, t = cr._fragments(sx, sy, sw, width, height)
    got = set(zip(k.tolist(), t.tolist()))
    want = set()
    for n in range(60):
        (ax, bx, cx), (ay, by, cy) = sx[n], sy[n]
        area = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(area) <= 1e-12:
            continue
        for y in range(height):
            for x in range(width):
                px, py = x + 0.5, y + 0.5
                l0 = ((bx - px) * (cy - py) - (by - py) * (cx - px)) / area
                l1 = ((cx - px) * (ay - py) - (cy - py) * (ax - px)) / area
                if l0 >= -1e-9 and l1 >= -1e-9 and 1 - l0 - l1 >= -1e-9:
                    want.add((y * width + x, n))
    # Pixel centres exactly on an edge may go either way.
    assert len(got ^ want) <= 0.01 * len(want)
    _, tri = cr._nearest(k, w, t, width * height)
    for pixel in range(0, width * height, 7):
        here = t[k == pixel]
        if len(here):
            assert tri[pixel] == here[np.argmax(w[k == pixel])]
        else:
            assert tri[pixel] == -1


def _quad(x0, x1, y0, y1, z) -> np.ndarray:
    """Two triangles of an axis-aligned quad in the plane z = `z`."""
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([[a, b, c], [a, c, d]], np.float32)


def _scene(quads: list[tuple[np.ndarray, int]], materials: list[cr.Material]) -> cr.Scene:
    pos = np.concatenate([q for q, _ in quads])
    nrm = np.concatenate([cr._face_normals(q) for q, _ in quads])
    return cr.Scene(
        pos,
        nrm,
        np.zeros(pos.shape[:2] + (2,), np.float32),
        np.full(pos.shape[:2], 0.5, np.float32),
        np.concatenate([np.full(len(q), m, np.int32) for q, m in quads]),
        materials,
    )


def test_camera_front_right():
    """The camera stands on the car's front right (DSF: +z forward, +x the car's
    left), so the nose is drawn on the right and the right side faces the camera."""
    view = cr._View(
        _scene([(_quad(-1, 1, 0, 1.4, 2.3), 0)], [cr.Material("paint", None, np.ones(3))]), cr.VANILLA_CAMERA, 400, 200
    )
    nose = view.project(np.array([[0.0, 0.5, 2.3]]))
    tail = view.project(np.array([[0.0, 0.5, -2.3]]))
    right_side = view.project(np.array([[-1.0, 0.5, 0.0]]))
    left_side = view.project(np.array([[1.0, 0.5, 0.0]]))
    assert nose[0][0] > tail[0][0]
    assert right_side[2][0] < left_side[2][0]  # nearer
    top, bottom = view.project(np.array([[0.0, 1.4, 0.0]])), view.project(np.array([[0.0, 0.0, 0.0]]))
    assert top[1][0] < bottom[1][0]  # up is up


def test_glass_shows_what_is_behind():
    """A glass pane half in front of a red panel: through the glass the panel shows,
    darker; beside it the glass alone is partly transparent; the panel alone is opaque."""
    paint = cr.Material("paint", None, np.array([0.8, 0.05, 0.05], np.float32))
    glass = cr.Material("glass", None, np.zeros(3, np.float32))
    # Seen from the front (yaw 0, pitch 0): z is towards the camera.
    scene = _scene([(_quad(-1, 1, 0, 1, 0.0), 0), (_quad(-0.2, 2, 0, 1, 0.5), 1)], [paint, glass])
    camera = cr.Camera(0.0, 0.0, 3.0)
    img = cr.render(scene, camera, 300, 200)
    view = cr._View(scene, camera, 300, 200)

    def pixel(x, y, z):
        sx, sy, _ = view.project(np.array([[x, y, z]]))
        return img[int(sy[0]), int(sx[0])].astype(int)

    panel, through, glass_only = pixel(-0.6, 0.5, 0.0), pixel(0.5, 0.5, 0.0), pixel(1.6, 0.5, 0.5)
    assert panel[3] == 255 and panel[0] > 3 * panel[1]
    assert through[3] == 255 and through[0] > 2 * through[1]  # the red shows through
    assert through[0] < panel[0]  # dimmed by the glass
    assert 60 < glass_only[3] < 240  # see-through


def test_fit_camera_finds_the_camera():
    """fit_camera recovers the camera a silhouette was drawn with (a box car)."""
    quads = []
    for z in (-2.2, 2.2):
        quads.append(_quad(-0.9, 0.9, 0, 1.3, z))
    box = np.concatenate(quads)
    side = box[:, :, [2, 1, 0]].copy()  # the same quads turned to face +-x
    side[:, :, 0] *= 0.9 / 2.2
    side[:, :, 2] *= 2.2 / 0.9
    roof = box[:, :, [0, 2, 1]].copy()
    roof[:, :, 1] = np.where(roof[:, :, 1] > 0, 1.3, 0.0)
    roof[:, :, 2] *= 2.2 / 1.3
    cabin = _quad(-0.7, 0.7, 1.3, 1.8, 0.4)
    pos = np.concatenate([box, side, roof, cabin])
    scene = _scene([(pos, 0)], [cr.Material("paint", None, np.ones(3))])
    truth = cr.Camera(36.0, 12.0, 1.3)
    target = cr.coverage(scene, truth, 360, 240)

    def score(cov):
        return float((cov & target).sum() / max(1, (cov | target).sum()))

    best, cam = cr.fit_camera(scene, score)
    assert best > 0.97
    assert abs(cam.yaw - truth.yaw) <= 2 and abs(cam.pitch - truth.pitch) <= 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok {name}")
