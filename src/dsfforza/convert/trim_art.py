"""Trim triangles that carry Forza artwork on the outside, moved to the car's atlas.

The converter draws Forza's trim techniques (misc, frame, matte_colors,
plastic2...) with DSF's shared trim material on one dark texel
(`forza2dsf.TRIM_UV`): right for the near-black swatches and plastics most of
them sample, wrong where a trim technique's UVs land on light artwork. The Alfa
Romeo GTA's quadrifoglio badges (a white triangle with the green cloverleaf on
each front wing, one on the tail) are `misc` triangles on opaque white and green
texels of `nodamage.xds`; drawn as trim they are solid black triangles
(research/forza-materials.md, "Trim artwork").

`split_trim_art` moves the trim triangles that can be seen from outside the car
and that the shared trim misdraws into subsections of their own
(classify_texels): light artwork to ART_TECHNIQUE[family] and dark detail (the
Cougar Eliminator's grille bars, flat black as trim) to DETAIL_TECHNIQUE,
both of which `forza_materials` classifies as textured, so `dsf_materials.Texturing`
draws them through the atlas (host Chassis or Lights material) like badges; flat
mid-grey to GREY_TECHNIQUE (one colour cell); bare metal to METAL_TECHNIQUE (the
shared chrome). Some of Forza's shaders scale the texel (PristinePaintScale, or
PaintScale in the ones without paint: fm.texel_scale, research/forza-materials.md
"Forza's trim shaders and flat black grilles"), so the classes see the scaled colours and the textured
subsections are named with the scale (fm.with_scale). Run it before the atlas is
planned.

- Artwork: at least ART_MIN_OPAQUE of the texel centres inside the triangle's UV
  triangle (its corners and centroid if it holds none) have alpha >= 128, and the
  brightest channel of their mean colour is ART_MIN_LEVEL or more (the shared
  trim's dark block is about 24). Mid-grey plastics stay trim (the DMC-12's and
  the Volt's bumpers, 73 to 90). Alpha-0 texels are paint-through filler, not
  trim colour (forza-materials.md).
- Seen: `seen_from_outside`, orthographic depth buffers of the LOD's body from
  around and above the car (glass doesn't hide anything). Light frames under the
  bonnet and in the boot (the Lancer Evo VI's 1.3 m2 of `frame` there) stay trim:
  no atlas room for them, and damage.split_exposed still projects the engine bay
  when the bonnet comes off.
- Techniques whose name says what they look like stay trim: black*, the
  underbody and the engine bay (KEEP_TRIM), and so do the plates (keeps_trim).

    split_trim_art(sections, atlases, lods) -> (sections, report)
"""

from __future__ import annotations

import dataclasses
import math

from dsfforza.convert import forza_materials as fm
from dsfforza.formats import carbin

ART_TECHNIQUE = {fm.NODAMAGE: "dsf_trim_art", fm.LIGHTS: "dsf_trim_art_lights"}
ART_MIN_OPAQUE = 0.5  # share of the triangle's texels with alpha >= ALPHA_OPAQUE
ART_MIN_LEVEL = 128  # brightest channel of the opaque texels' mean RGB
ALPHA_OPAQUE = 128
# Light artwork that is bare metal (is_metal) takes DSF's shared chrome instead of the
# atlas, as Forza's chrome techniques do (research/forza-materials.md, "Chrome trim"):
# on the host Chassis material (specular 0.03, no reflection) the Veyron's grey 177
# scoops read as flat white in game. Each triangle on the chrome texel nearest its Forza
# grey as drawn (metal_grey; forza2dsf.CHROME_GREYS, vanilla's chrome greys), named
# "dsf_trim_metal#<grey>" (fm.with_grey), not one shared grey for every car.
METAL_TECHNIQUE = "dsf_trim_metal"
METAL_WORDS = ("metal", "alum", "steel")
METAL_MAX_CHROMA = 24  # brightest minus darkest channel of the mean colour
METAL_MAX_SPREAD = 12  # standard deviation of the texels' luminance
METAL_MAX_LEVEL = 240  # white is paint
# Dark trims that aren't one flat near-black (research/forza-materials.md, "Flat black
# grilles"). Forza draws every trim texel times the technique's texel scale
# (fm.texel_scale) times its lighting; the shared trim is one dark texel (about 24) for
# all of them, which keeps the outline of a grille and loses its bars. On the texels
# after the scale:
# - detail: around the triangle's texels the atlas has contrast. Per texel, the
#   luminance's maximum and minimum over a window of DETAIL_WINDOW texels
#   (local_contrast); the median texel's maximum is DETAIL_MIN_LEVEL or more and lies
#   DETAIL_MIN_SPREAD or more above its minimum (the Cougar Eliminator's hidden-headlamp
#   doors: bars of 40 to 90 three texels apart on black, drawn by triangles too small
#   to hold a bar each): through the atlas like artwork (DETAIL_TECHNIQUE);
# - grey: one flat colour whose brightest channel is GREY_MIN_LEVEL or more (the Volt's
#   and the DMC-12's mid-grey bumpers): a flat colour cell (GREY_TECHNIQUE);
# - darker and flat stays on the shared trim, which looks the same.
DETAIL_TECHNIQUE = "dsf_trim_detail"
GREY_TECHNIQUE = "dsf_trim_grey"
# Flat dark trims whose Forza technique has a specular or a reflection (fm.gloss_params:
# the Cougar Eliminator's satin-black scoop, `misc` on texels of 2 to 7): one colour
# cell each, drawn with a gloss material (dsf_materials.GLOSS) instead of the shared
# trim, which has next to no specular (research/forza-materials.md, "Gloss"). Swatch
# subsections (one texel for the whole subsection) are taken too, by triangle.
GLOSS_TECHNIQUE = "dsf_trim_gloss"
DETAIL_WINDOW = 5  # texels (odd)
DETAIL_MIN_LEVEL = 40
DETAIL_MIN_SPREAD = 20
GREY_MIN_LEVEL = 48
KEEP_TRIM = frozenset({"undercarriage", "engine", "cage", "bottom", "bottom_2"})
# black, black_glass, black_glass_breakable; mirror glass (a render target in Forza).
KEEP_TRIM_PREFIX = ("black", "mirror")

# seen_from_outside: views around the car (azimuth step VIEW_AZIMUTH_STEP degrees)
# level and from VIEW_ELEVATION above, plus one from straight above; none from below.
VIEW_AZIMUTH_STEP = 45
VIEW_ELEVATION = 35
VIEW_PIXEL = 0.02  # m per depth-buffer pixel
VIEW_SLACK = 0.02  # m: a point this far behind what its pixel shows still counts as seen


def triangle_cells(shape, pts):
    """(rows, columns) index arrays of the texels whose centres lie inside a
    texel-space triangle of an image of `shape` (height, width); its corners'
    and centroid's texels if it covers no centre; none for non-finite UVs."""
    import numpy as np

    height, width = shape[:2]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    if not all(math.isfinite(c) for c in xs + ys):  # non-finite UVs (a GMC_YukonXL_01 trim): no texels
        return np.zeros(0, dtype=int), np.zeros(0, dtype=int)
    x0, x1 = max(0, int(math.floor(min(xs)))), min(width, int(math.ceil(max(xs))) + 1)
    y0, y1 = max(0, int(math.floor(min(ys)))), min(height, int(math.ceil(max(ys))) + 1)
    area = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (ys[1] - ys[0]) * (xs[2] - xs[0])
    if x0 < x1 and y0 < y1 and abs(area) > 1e-12:
        gy, gx = np.arange(y0, y1)[:, None] + 0.5, np.arange(x0, x1) + 0.5  # np.mgrid's values, broadcast
        w0 = ((xs[1] - gx) * (ys[2] - gy) - (ys[1] - gy) * (xs[2] - gx)) / area
        w1 = ((xs[2] - gx) * (ys[0] - gy) - (ys[2] - gy) * (xs[0] - gx)) / area
        inside = (w0 >= 0) & (w1 >= 0) & (w0 + w1 <= 1)
        if inside.any():
            iy, ix = np.nonzero(inside)
            return iy + y0, ix + x0
    points = list(zip(xs, ys)) + [(sum(xs) / 3, sum(ys) / 3)]
    return (
        np.array([min(height - 1, max(0, int(y))) for _, y in points], dtype=int),
        np.array([min(width - 1, max(0, int(x))) for x, _ in points], dtype=int),
    )


CELLS_CHUNK = 1 << 21  # triangles_cells: bounding-box texels tested at a time
CELLS_ONE_BY_ONE = 256  # triangles_cells: boxes of more texels go through triangle_cells


def triangles_cells(shape, pts):
    """triangle_cells of many texel-space triangles at once (pts: (n, 3, 2)): the
    (rows, columns) of every triangle's texels one after the other, each triangle's in
    triangle_cells' order, and the offsets (n + 1) where each triangle's start; the same
    arithmetic per texel, without a numpy call per triangle. Triangles whose box holds more
    than CELLS_ONE_BY_ONE texels go through triangle_cells itself (its broadcast grid costs
    less per texel there)."""
    import numpy as np

    height, width = shape[:2]
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 3, 2)
    n = len(pts)
    finite = np.isfinite(pts).all((1, 2))  # non-finite UVs (a GMC_YukonXL_01 trim): no texels
    pts = np.where(finite[:, None, None], pts, 0.0)
    xs, ys = pts[:, :, 0], pts[:, :, 1]
    # The box (x0, x1) x (y0, y1), clamped as triangle_cells does (clipped first: no overflow).
    x0 = np.maximum(0, np.clip(np.floor(xs.min(1)), -1, width + 1)).astype(np.int64)
    x1 = np.minimum(width, np.clip(np.ceil(xs.max(1)), -2, width) + 1).astype(np.int64)
    y0 = np.maximum(0, np.clip(np.floor(ys.min(1)), -1, height + 1)).astype(np.int64)
    y1 = np.minimum(height, np.clip(np.ceil(ys.max(1)), -2, height) + 1).astype(np.int64)
    area = (xs[:, 1] - xs[:, 0]) * (ys[:, 2] - ys[:, 0]) - (ys[:, 1] - ys[:, 0]) * (xs[:, 2] - xs[:, 0])
    boxed = finite & (x0 < x1) & (y0 < y1) & (np.abs(area) > 1e-12)
    bw = np.where(boxed, x1 - x0, 0)
    size = bw * np.where(boxed, y1 - y0, 0)
    count = np.zeros(n, np.int64)
    rows, cols, owner = [], [], []
    for k in np.nonzero(size > CELLS_ONE_BY_ONE)[0].tolist():
        r, c = triangle_cells(shape, [tuple(p) for p in pts[k].tolist()])
        rows.append(r)
        cols.append(c)
        owner.append(np.full(len(r), k, np.int64))
        count[k] = len(r)
    size = np.where(size > CELLS_ONE_BY_ONE, 0, size)
    done = 0
    while done < n:  # triangles in chunks of about CELLS_CHUNK box texels
        end = done + max(1, int(np.searchsorted(np.cumsum(size[done:]), CELLS_CHUNK, "right")))
        sel = np.arange(done, end)[size[done:end] > 0]
        done = end
        if not len(sel):
            continue
        k = size[sel]
        tri = np.repeat(sel, k)
        local = np.arange(int(k.sum())) - np.repeat(np.cumsum(k) - k, k)
        w = bw[tri]
        gy = (local // w + y0[tri]) + 0.5
        gx = (local % w + x0[tri]) + 0.5
        X, Y, a = xs[tri], ys[tri], area[tri]
        w0 = ((X[:, 1] - gx) * (Y[:, 2] - gy) - (Y[:, 1] - gy) * (X[:, 2] - gx)) / a
        w1 = ((X[:, 2] - gx) * (Y[:, 0] - gy) - (Y[:, 2] - gy) * (X[:, 0] - gx)) / a
        inside = np.nonzero((w0 >= 0) & (w1 >= 0) & (w0 + w1 <= 1))[0]
        rows.append((gy[inside] - 0.5).astype(np.int64))
        cols.append((gx[inside] - 0.5).astype(np.int64))
        owner.append(tri[inside])
        count += np.bincount(tri[inside], minlength=n)
    # A finite triangle with no texel centre inside: its corners' and centroid's texels.
    corner = np.nonzero(finite & (count == 0))[0]
    if len(corner):
        cx = np.concatenate([xs[corner], ((xs[corner, 0] + xs[corner, 1]) + xs[corner, 2])[:, None] / 3], 1)
        cy = np.concatenate([ys[corner], ((ys[corner, 0] + ys[corner, 1]) + ys[corner, 2])[:, None] / 3], 1)
        # int() truncates towards 0, then the clamp: clipped first, the same texel
        rows.append(np.minimum(height - 1, np.maximum(0, np.trunc(np.clip(cy, -1, height)).astype(np.int64))).ravel())
        cols.append(np.minimum(width - 1, np.maximum(0, np.trunc(np.clip(cx, -1, width)).astype(np.int64))).ravel())
        owner.append(np.repeat(corner, 4))
        count[corner] = 4
    if not owner:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(n + 1, np.int64)
    owner = np.concatenate(owner)
    order = np.argsort(owner, kind="stable")  # by triangle, each one's texels in the order found
    offsets = np.zeros(n + 1, np.int64)
    np.cumsum(count, out=offsets[1:])
    return np.concatenate(rows)[order], np.concatenate(cols)[order], offsets


def texel_points(m, pool, tris, width: int, height: int):
    """(n, 3, 2) texel-space corners of triangles `tris` (indices into `pool`) of a subsection
    resolved as `m` (fm.SubMaterial): m.uv of each vertex's UV0 times the image size, each
    vertex's m.uv worked out once."""
    import numpy as np

    index = np.array(tris, dtype=np.int64).reshape(-1, 3)
    used, inverse = np.unique(index, return_inverse=True)
    uv = np.array([m.uv(pool[i].uv0) for i in used.tolist()], dtype=np.float64).reshape(-1, 2)
    pts = uv[inverse.reshape(-1)].reshape(-1, 3, 2)
    pts[:, :, 0] *= width
    pts[:, :, 1] *= height
    return pts


def triangle_texels(image, pts):
    """RGBA rows (numpy, int) of the texels whose centres lie inside a texel-space
    triangle; its corners' and centroid's texels if it covers no centre."""
    ys, xs = triangle_cells(image.shape, pts)
    return image[ys, xs].astype(int)


def triangles_texels(image, pts):
    """triangle_texels of many triangles (triangles_cells): the RGBA rows of all of them one
    after the other and the offsets where each triangle's start."""
    ys, xs, offsets = triangles_cells(image.shape, pts)
    return image[ys, xs].astype(int), offsets


def is_artwork(texels) -> bool:
    """Whether these texels (triangle_texels) are light opaque artwork, not dark trim."""
    opaque = texels[texels[:, 3] >= ALPHA_OPAQUE]
    if len(opaque) == 0 or len(opaque) < ART_MIN_OPAQUE * len(texels):
        return False
    return float(opaque[:, :3].mean(0).max()) >= ART_MIN_LEVEL


def is_metal(technique: str, texels) -> bool:
    """Whether artwork texels (is_artwork) are bare metal rather than paint: a
    technique named for metal, or one flat (luminance spread METAL_MAX_SPREAD or
    less) neutral (chroma METAL_MAX_CHROMA or less) grey below METAL_MAX_LEVEL.
    Forza draws its polished parts this way: the Veyron's roof scoops and engine
    cover are `misc` triangles on one texel of grey 177, its fuel cap and
    window frames `frame` on grey 227, the Crossfire's vent strakes `misc` on a
    strip of (205, 200, 191). Painted artwork has colour (the quadrifoglio's
    green), detail (lettering) or is white."""
    n = technique.lower()
    if any(word in n for word in METAL_WORDS):
        return True
    opaque = texels[texels[:, 3] >= ALPHA_OPAQUE][:, :3].astype(float)
    if len(opaque) == 0:
        return False
    mean = opaque.mean(0)
    lum = opaque @ (0.299, 0.587, 0.114)
    return (
        float(mean.max() - mean.min()) <= METAL_MAX_CHROMA
        and float(lum.std()) <= METAL_MAX_SPREAD
        and float(mean.max()) < METAL_MAX_LEVEL
    )


ART, METAL, DETAIL, GREY, GLOSS = "art", "metal", "detail", "grey", "gloss"


def metal_grey(texels, scale: float = 1.0) -> float:
    """The grey level (sRGB, 0..255) Forza draws bare-metal texels (triangle_texels) at:
    the luminance of their opaque texels' mean, times the technique's texel scale in
    linear light (fm.scale_texels: the Veyron's 227 `frame` at PristinePaintScale 0.3 is
    drawn at 133)."""
    import numpy as np

    opaque = texels[texels[:, 3] >= ALPHA_OPAQUE]
    rows = opaque if len(opaque) else texels
    mean = rows[:, :3].astype(float).mean(0)
    lum = float(np.dot(fm.scale_texels(mean, scale), (0.299, 0.587, 0.114)))
    return min(255.0, max(0.0, lum))


def local_contrast(image):
    """(height, width, 2) int: per texel the largest and the smallest luminance of the
    opaque texels in the DETAIL_WINDOW square around it (transparent texels count as
    the texel itself)."""
    import numpy as np

    lum = np.rint(image[..., :3].astype(np.float64) @ (0.299, 0.587, 0.114)).astype(np.int64)
    opaque = image[..., 3] >= ALPHA_OPAQUE
    r = DETAIL_WINDOW // 2
    h, w = lum.shape
    # The window's max (min) as the max over its rows of the max over its columns: the same
    # values (-1..256 fit int16), a tenth of the passes.
    out = []
    for src, reduce in ((np.where(opaque, lum, -1), np.maximum), (np.where(opaque, lum, 256), np.minimum)):
        padded = np.pad(src.astype(np.int16), r, mode="edge")
        rows = padded[:h].copy()
        for dy in range(1, DETAIL_WINDOW):
            reduce(rows, padded[dy : dy + h], out=rows)
        win = rows[:, :w].copy()
        for dx in range(1, DETAIL_WINDOW):
            reduce(win, rows[:, dx : dx + w], out=win)
        out.append(win.astype(np.int64))
    hi, lo = out
    hi = np.where(hi < 0, lum, hi)
    lo = np.where(lo > 255, lum, lo)
    return np.stack((hi, lo), axis=-1)


def with_contrast(image):
    """The atlas (h, w, 4) with local_contrast's two channels appended: triangle_texels
    of it gives the rows classify_texels reads."""
    import numpy as np

    return np.concatenate((image.astype(np.int64), local_contrast(image)), axis=-1)


def classify_texels(technique: str, texels, scale: float = 1.0) -> str | None:
    """What a trim triangle on these texels (triangle_texels) becomes: ART (light
    artwork, the atlas), METAL (bare metal, the shared chrome), DETAIL (dark with
    detail, the atlas), GREY (one flat mid-grey, a colour cell) or None (stays on the
    shared trim). `scale` is the technique's texel scale (fm.texel_scale): Forza draws
    the texels that much darker (linear light), so the classes after the metal test see
    the scaled colours. DETAIL needs rows with local_contrast's columns (with_contrast).
    Bare metal is judged on the texels as painted (the Veyron's 177-grey `frame`
    parts at PristinePaintScale 0.3 with a specular: a polished metal, not a dark
    grey)."""
    import numpy as np

    opaque = texels[texels[:, 3] >= ALPHA_OPAQUE]
    if len(opaque) == 0 or len(opaque) < ART_MIN_OPAQUE * len(texels):
        return None
    if is_artwork(texels) and is_metal(technique, texels):
        return METAL
    rgb = fm.scale_texels(opaque[:, :3], scale)
    level = float(rgb.mean(0).max())
    if level >= ART_MIN_LEVEL:
        return ART
    if texels.shape[1] >= 6:  # with_contrast rows
        rows = texels[texels[:, 3] >= ALPHA_OPAQUE]
        high = fm.scale_texels(rows[:, 4], scale)
        low = fm.scale_texels(rows[:, 5], scale)
        if np.median(high) >= DETAIL_MIN_LEVEL and np.median(high - low) >= DETAIL_MIN_SPREAD:
            return DETAIL
    if level >= GREY_MIN_LEVEL:
        return GREY
    return None


def has_gloss(settings, technique: str) -> bool:
    """Whether Forza gives this trim technique a specular or a reflection that DSF's gloss
    materials draw (fm.gloss_params with the car's ShaderSettings, dsf_materials.gloss_material):
    `weatherstrip` (SpecularAmount 0.3, no reflection) has too little to draw and stays."""
    from dsfforza.convert import dsf_materials

    g = fm.gloss_params(settings, technique)
    return g is not None and dsf_materials.gloss_material(g, 255) is not None


def is_opaque(texels) -> bool:
    """Half the texels or more opaque: trim colour, not paint-through filler."""
    return len(texels) > 0 and int((texels[:, 3] >= ALPHA_OPAQUE).sum()) >= ART_MIN_OPAQUE * len(texels)


def keeps_trim(technique: str) -> bool:
    """Techniques left as they are: KEEP_TRIM, KEEP_TRIM_PREFIX, and the plates
    (plates.apply replaces Forza's numberplate quads by name; numplate is the plate
    lamp strip)."""
    n = technique.lower()
    return n in KEEP_TRIM or n.startswith(KEEP_TRIM_PREFIX) or "plate" in n


def _view_directions():
    """Unit vectors from the car towards the cameras (Forza space, +y up)."""
    out = [(0.0, 1.0, 0.0)]
    for elevation in (0, VIEW_ELEVATION):
        e = math.radians(elevation)
        for k in range(0, 360, VIEW_AZIMUTH_STEP):
            a = math.radians(k)
            out.append((math.cos(e) * math.sin(a), math.sin(e), math.cos(e) * math.cos(a)))
    return out


def _surface_points(tris, spacing: float):
    """Points on triangles (n, 3, 3) no farther apart than about `spacing`. Each
    triangle ABC (AB its longest edge, C opposite) is swept by rows parallel to AB,
    `spacing` apart over its height, each row sampled `spacing` apart:
    P = (1 - t) (A + s (B - A)) + t C. About 2 x area / spacing^2 points plus
    its edges, so long thin slivers cost little."""
    import numpy as np

    if len(tris) == 0:
        return np.zeros((0, 3))
    lengths = np.linalg.norm(tris - np.roll(tris, -1, axis=1), axis=2)  # edge k: vertex k -> k + 1
    k = lengths.argmax(1)
    rows = np.arange(len(tris))
    a, b, c = tris[rows, k], tris[rows, (k + 1) % 3], tris[rows, (k + 2) % 3]
    base = lengths[rows, k]
    height = np.linalg.norm(np.cross(b - a, c - a), axis=1) / np.maximum(base, 1e-12)
    k1 = np.clip(np.ceil(base / spacing), 1, 400).astype(np.int64)
    k2 = np.clip(np.ceil(height / spacing), 1, 400).astype(np.int64)
    counts = (k1 + 1) * (k2 + 1)
    tri = np.repeat(rows, counts)
    local = np.arange(int(counts.sum())) - np.repeat(np.cumsum(counts) - counts, counts)
    s = (local % (k1 + 1)[tri]) / k1[tri]
    t = (local // (k1 + 1)[tri]) / k2[tri]
    s, t = s[:, None], t[:, None]
    # (1 - t) * (a + s * (b - a)) + t * c, in place (each product and sum as written:
    # x * y and x + y are the same either way round)
    at = a[tri]
    out = b[tri]
    out -= at
    out *= s
    out += at
    out *= 1 - t
    ct = c[tri]
    ct *= t
    out += ct
    return out


def seen_from_outside(occluders, candidates):
    """For each candidate triangle (array (n, 3, 3), Forza space), whether a point
    of it (centroid, or a corner pulled a quarter of the way in) is in front of or
    within VIEW_SLACK of what an orthographic depth buffer of `occluders` (array
    (m, 3, 3), the candidates among them) shows, from any of the views of
    _view_directions. A candidate is seen if any view sees it, so the views go in an
    order of their own: those whose screen x axis (`right`) comes out the same, bit for bit
    (each azimuth level and from above), one after the other, and the second reuses the
    first's pixel columns."""
    import numpy as np

    if len(candidates) == 0:
        return np.zeros(0, bool)
    surface = np.concatenate([_surface_points(occluders, VIEW_PIXEL * 0.7), candidates.reshape(-1, 3)])
    centroid = candidates.mean(1, keepdims=True)
    probes = np.concatenate([centroid, candidates + (centroid - candidates) * 0.25], axis=1)  # (n, 4, 3)
    seen = np.zeros(len(candidates), bool)
    views = []
    for d in _view_directions():
        d = np.array(d)
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.99 else np.array([0.0, 0.0, 1.0])
        right = np.cross(up, d)
        right /= np.linalg.norm(right)
        views.append((d, right, np.cross(d, right)))
    first = {}  # right's bytes -> its first view's place
    order = sorted(range(len(views)), key=lambda k: (first.setdefault(views[k][1].tobytes(), k), k))
    columns = None  # (right's bytes, x0, w, each point's pixel column) of the last view
    for d, right, up in (views[k] for k in order):
        if columns is not None and columns[0] == right.tobytes():
            _, x0, w, ix = columns
        else:
            sx = surface @ right
            x0 = sx.min()
            w = int((sx.max() - x0) / VIEW_PIXEL) + 1
            sx -= x0  # the pixel of each point, ((sx - x0) / VIEW_PIXEL) as int, in place
            sx /= VIEW_PIXEL
            ix = sx.astype(int)
            columns = (right.tobytes(), x0, w, ix)
        sy, depth = surface @ up, surface @ d
        np.negative(depth, out=depth)  # smaller depth: nearer the camera
        y0 = sy.min()
        h = int((sy.max() - y0) / VIEW_PIXEL) + 1
        sy -= y0
        sy /= VIEW_PIXEL
        flat = sy.astype(int)
        flat *= w
        flat += ix
        buffer = np.full(w * h, np.inf)
        np.minimum.at(buffer, flat, depth)
        buffer = buffer.reshape(h, w)
        # Pixels the points missed take the nearest of their neighbours (no see-through gaps).
        holes = np.isinf(buffer)
        if holes.any():
            padded = np.pad(buffer, 1, constant_values=np.inf)
            near = padded[:h, :w].copy()
            for dy, dx in ((dy, dx) for dy in range(3) for dx in range(3) if dy or dx):
                np.minimum(near, padded[dy : dy + h, dx : dx + w], out=near)
            buffer[holes] = near[holes]
        todo = ~seen
        p = probes[todo].reshape(-1, 3)
        px = np.clip(((p @ right - x0) / VIEW_PIXEL).astype(int), 0, w - 1)
        py = np.clip(((p @ up - y0) / VIEW_PIXEL).astype(int), 0, h - 1)
        visible = -(p @ d) <= buffer[py, px] + VIEW_SLACK
        seen[np.flatnonzero(todo)] = visible.reshape(-1, probes.shape[1]).any(1)
        if seen.all():
            break
    return seen


def lod_triangles(sections, lod):
    """Every triangle at `lod` that hides what lies behind it (all but glass and
    skipped techniques), as an array (n, 3, 3)."""
    import numpy as np

    out = []
    pools = {}  # id(vertex list) -> (the list, kept so the id stays its own; positions)
    for section in sections:
        for sub in section.subsections:
            if sub.lod != lod or fm.classify(sub.material)[0] in (fm.GLASS, fm.SKIP):
                continue
            pool = section.vertices_for(sub)
            if id(pool) not in pools:  # a section's subsections share its pool: converted once
                pools[id(pool)] = (pool, np.array([v.position for v in pool], dtype=np.float64).reshape(-1, 3))
            pool = pools[id(pool)][1]
            tris = np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)
            if len(tris):
                out.append(pool[tris])
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def split_trim_art(sections, atlases: fm.AtlasCache, lods=None, settings: dict | None = None):
    """Sections with every trim triangle that classify_texels sends off the shared
    trim and that is seen from outside (seen_from_outside) moved into a subsection
    of its own at the same LOD with the same UV transform: ART_TECHNIQUE[family]
    (light artwork), METAL_TECHNIQUE (bare metal), DETAIL_TECHNIQUE (dark detail)
    or GREY_TECHNIQUE (flat mid-grey), the textured ones named with the
    technique's texel scale (fm.with_scale; `settings`: the car's ShaderSettings,
    read from the atlases' folder when None). The other triangles keep theirs.
    Only textured trims of the main atlases at `lods` (None: all); on the Lights
    atlas only artwork, unscaled. Bare metal goes into one subsection per chrome grey
    (forza2dsf.chrome_grey of metal_grey: METAL_TECHNIQUE#<grey>). Unchanged sections
    are returned as they are. Report: per section/technique, the triangles moved, how
    many of them as metal (and per chrome grey, metal_greys), detail and grey, and their
    area (m2)."""
    import numpy as np

    from dsfforza.convert import forza2dsf

    if settings is None:
        car_dir = getattr(atlases, "car_dir", None)
        try:
            settings = fm.shader_settings(car_dir) if car_dir is not None else {}
        except OSError:
            settings = {}
    images = {}
    # (section index, subsection index) -> (family, scale, [triangle index], [triangle], {triangle index: class})
    found: dict = {}
    for si, section in enumerate(sections):
        for ui, sub in enumerate(section.subsections):
            if lods is not None and sub.lod not in lods:
                continue
            m = fm.resolve(section, sub)
            if m.role != fm.TRIM or m.kind not in ("textured", "swatch") or m.family not in ART_TECHNIQUE:
                continue
            if keeps_trim(sub.material):
                continue
            glossy = m.family == fm.NODAMAGE and has_gloss(settings, sub.material)
            if m.kind == "swatch" and not glossy:
                continue
            if m.family not in images:
                try:
                    width, height, rgba = atlases.get(m.family)
                except (FileNotFoundError, OSError):
                    images[m.family] = None
                else:
                    image = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(height, width, 4)
                    images[m.family] = with_contrast(image) if m.family == fm.NODAMAGE else image
            image = images[m.family]
            if image is None:
                continue
            scale = fm.texel_scale(settings, sub.material) if m.family == fm.NODAMAGE else 1.0
            height, width = image.shape[:2]
            pool = section.vertices_for(sub)
            tris = sub.triangles()
            every, offsets = triangles_texels(image, texel_points(m, pool, tris, width, height))
            for k, tri in enumerate(tris):
                texels = every[offsets[k] : offsets[k + 1]]
                if m.kind == "swatch":  # one texel for the whole subsection: only its gloss
                    kind = GLOSS if is_opaque(texels) else None
                else:
                    kind = classify_texels(sub.material, texels, scale)
                    if kind is None and glossy and is_opaque(texels):
                        kind = GLOSS
                if kind is None or (m.family != fm.NODAMAGE and kind in (DETAIL, GREY)):
                    continue
                entry = found.setdefault((si, ui), (m.family, scale, [], [], {}))
                entry[2].append(k)
                entry[3].append([pool[i].position for i in tri])
                entry[4][k] = (METAL, forza2dsf.chrome_grey(metal_grey(texels, scale))) if kind == METAL else kind
    if not found:
        return list(sections), {}
    moved: dict = {}  # (si, ui) -> set of triangle indices
    for lod in sorted({sections[si].subsections[ui].lod for si, ui in found}):
        keys = [key for key in found if sections[key[0]].subsections[key[1]].lod == lod]
        candidates = np.array([t for key in keys for t in found[key][3]], dtype=np.float64).reshape(-1, 3, 3)
        seen = seen_from_outside(lod_triangles(sections, lod), candidates)
        n = 0
        for key in keys:
            count = len(found[key][2])
            picked = {k for k, s in zip(found[key][2], seen[n : n + count]) if s}
            n += count
            if picked:
                moved[key] = picked
    out, report = [], {}
    for si, section in enumerate(sections):
        if not any(key[0] == si for key in moved):
            out.append(section)
            continue
        subs = []
        for ui, sub in enumerate(section.subsections):
            picked = moved.get((si, ui))
            if not picked:
                subs.append(sub)
                continue
            family, scale, _, _, kinds = found[(si, ui)]
            keep, parts, area = [], {ART: [], DETAIL: [], GREY: [], GLOSS: []}, 0.0
            pool = section.vertices_for(sub)
            for k, tri in enumerate(sub.triangles()):
                if k not in picked:
                    keep += tri
                    continue
                parts.setdefault(kinds[k], []).extend(tri)
                p = np.array([pool[i].position for i in tri])
                area += 0.5 * float(np.linalg.norm(np.cross(p[1] - p[0], p[2] - p[0])))
            if keep:
                subs.append(dataclasses.replace(sub, index_type=4, indices=keep))
            # The textured classes keep the Forza technique they came from (its gloss).
            names = {
                ART: fm.with_source(fm.with_scale(ART_TECHNIQUE[family], scale), sub.material),
                DETAIL: fm.with_source(fm.with_scale(DETAIL_TECHNIQUE, scale), sub.material),
                GREY: fm.with_source(fm.with_scale(GREY_TECHNIQUE, scale), sub.material),
                GLOSS: fm.with_source(fm.with_scale(GLOSS_TECHNIQUE, scale), sub.material),
            }
            metal = sorted(kind for kind in parts if isinstance(kind, tuple))  # (METAL, grey), darkest first
            for kind in [ART, *metal, DETAIL, GREY, GLOSS]:
                if parts[kind]:
                    name = fm.with_grey(METAL_TECHNIQUE, kind[1]) if kind in metal else names[kind]
                    subs.append(carbin.SubSection(name, sub.lod, 4, sub.uv_transform, parts[kind]))
            stats = report.setdefault(
                f"{section.name}/{sub.material}",
                {"triangles": 0, "metal": 0, "detail": 0, "grey": 0, "gloss": 0, "area": 0.0},
            )
            stats["triangles"] += sum(len(v) for v in parts.values()) // 3
            stats["metal"] += sum(len(parts[kind]) for kind in metal) // 3
            for kind in metal:
                greys = stats.setdefault("metal_greys", {})
                greys[str(kind[1])] = greys.get(str(kind[1]), 0) + len(parts[kind]) // 3
            stats["detail"] += len(parts[DETAIL]) // 3
            stats["grey"] += len(parts[GREY]) // 3
            stats["gloss"] += len(parts[GLOSS]) // 3
            stats["area"] += area
        out.append(dataclasses.replace(section, subsections=subs))
    return out, report
