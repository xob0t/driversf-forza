"""Trim triangles that carry Forza artwork on the outside, moved to the car's atlas.

The converter draws Forza's trim techniques (misc, frame, matte_colors,
plastic2...) with DSF's shared trim material on one dark texel
(`forza2dsf.TRIM_UV`): right for the near-black swatches and plastics most of
them sample, wrong where a trim technique's UVs land on light artwork. The Alfa
Romeo GTA's quadrifoglio badges (a white triangle with the green cloverleaf on
each front wing, one on the tail) are `misc` triangles on opaque white and green
texels of `nodamage.xds`; drawn as trim they were solid black triangles (user
report on out/final-gui2, research/forza-materials.md, "Trim artwork").

`split_trim_art` moves the trim triangles that sample light opaque artwork and
can be seen from outside the car into a subsection of their own with technique
ART_TECHNIQUE[family], which `forza_materials` classifies as textured:
`dsf_materials.Texturing` then draws them through the atlas (host Chassis or
Lights material) like badges. Run it before the atlas is planned.

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
KEEP_TRIM = frozenset({"undercarriage", "engine", "cage", "bottom", "bottom_2"})
# black, black_glass, black_glass_breakable; mirror glass (a render target in Forza).
KEEP_TRIM_PREFIX = ("black", "mirror")

# seen_from_outside: views around the car (azimuth step VIEW_AZIMUTH_STEP degrees)
# level and from VIEW_ELEVATION above, plus one from straight above; none from below.
VIEW_AZIMUTH_STEP = 45
VIEW_ELEVATION = 35
VIEW_PIXEL = 0.02  # m per depth-buffer pixel
VIEW_SLACK = 0.02  # m: a point this far behind what its pixel shows still counts as seen


def triangle_texels(image, pts):
    """RGBA rows (numpy, int) of the texels whose centres lie inside a texel-space
    triangle; its corners' and centroid's texels if it covers no centre."""
    import numpy as np

    height, width = image.shape[:2]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    x0, x1 = max(0, int(math.floor(min(xs)))), min(width, int(math.ceil(max(xs))) + 1)
    y0, y1 = max(0, int(math.floor(min(ys)))), min(height, int(math.ceil(max(ys))) + 1)
    area = (xs[1] - xs[0]) * (ys[2] - ys[0]) - (ys[1] - ys[0]) * (xs[2] - xs[0])
    if x0 < x1 and y0 < y1 and abs(area) > 1e-12:
        gy, gx = np.mgrid[y0:y1, x0:x1] + 0.5
        w0 = ((xs[1] - gx) * (ys[2] - gy) - (ys[1] - gy) * (xs[2] - gx)) / area
        w1 = ((xs[2] - gx) * (ys[0] - gy) - (ys[2] - gy) * (xs[0] - gx)) / area
        inside = (w0 >= 0) & (w1 >= 0) & (w0 + w1 <= 1)
        if inside.any():
            return image[y0:y1, x0:x1][inside].astype(int)
    points = list(zip(xs, ys)) + [(sum(xs) / 3, sum(ys) / 3)]
    return np.array(
        [image[min(height - 1, max(0, int(y))), min(width - 1, max(0, int(x)))] for x, y in points], dtype=int
    )


def is_artwork(texels) -> bool:
    """Whether these texels (triangle_texels) are light opaque artwork, not dark trim."""
    opaque = texels[texels[:, 3] >= ALPHA_OPAQUE]
    if len(opaque) == 0 or len(opaque) < ART_MIN_OPAQUE * len(texels):
        return False
    return float(opaque[:, :3].mean(0).max()) >= ART_MIN_LEVEL


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
    return (1 - t) * (a[tri] + s * (b[tri] - a[tri])) + t * c[tri]


def seen_from_outside(occluders, candidates):
    """For each candidate triangle (array (n, 3, 3), Forza space), whether a point
    of it (centroid, or a corner pulled a quarter of the way in) is in front of or
    within VIEW_SLACK of what an orthographic depth buffer of `occluders` (array
    (m, 3, 3), the candidates among them) shows, from any of the views of
    _view_directions."""
    import numpy as np

    if len(candidates) == 0:
        return np.zeros(0, bool)
    surface = _surface_points(occluders, VIEW_PIXEL * 0.7)
    centroid = candidates.mean(1, keepdims=True)
    probes = np.concatenate([centroid, candidates + (centroid - candidates) * 0.25], axis=1)  # (n, 4, 3)
    seen = np.zeros(len(candidates), bool)
    for d in _view_directions():
        d = np.array(d)
        up = np.array([0.0, 1.0, 0.0]) if abs(d[1]) < 0.99 else np.array([0.0, 0.0, 1.0])
        right = np.cross(up, d)
        right /= np.linalg.norm(right)
        up = np.cross(d, right)
        sx, sy, depth = surface @ right, surface @ up, -(surface @ d)  # smaller depth: nearer the camera
        x0, y0 = sx.min(), sy.min()
        w = int((sx.max() - x0) / VIEW_PIXEL) + 1
        h = int((sy.max() - y0) / VIEW_PIXEL) + 1
        ix = ((sx - x0) / VIEW_PIXEL).astype(int)
        iy = ((sy - y0) / VIEW_PIXEL).astype(int)
        buffer = np.full(w * h, np.inf)
        np.minimum.at(buffer, iy * w + ix, depth)
        buffer = buffer.reshape(h, w)
        # Pixels the points missed take the nearest of their neighbours (no see-through gaps).
        holes = np.isinf(buffer)
        if holes.any():
            padded = np.pad(buffer, 1, constant_values=np.inf)
            near = np.min([padded[dy : dy + h, dx : dx + w] for dy in range(3) for dx in range(3)], axis=0)
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


def _lod_triangles(sections, lod):
    """Every triangle at `lod` that hides what lies behind it (all but glass and
    skipped techniques), as an array (n, 3, 3)."""
    import numpy as np

    out = []
    for section in sections:
        for sub in section.subsections:
            if sub.lod != lod or fm.classify(sub.material)[0] in (fm.GLASS, fm.SKIP):
                continue
            pool = np.array([v.position for v in section.vertices_for(sub)], dtype=np.float64).reshape(-1, 3)
            tris = np.array(sub.triangles(), dtype=np.int64).reshape(-1, 3)
            if len(tris):
                out.append(pool[tris])
    return np.concatenate(out) if out else np.zeros((0, 3, 3))


def split_trim_art(sections, atlases: fm.AtlasCache, lods=None):
    """Sections with every trim triangle that samples light opaque artwork
    (is_artwork) and is seen from outside (seen_from_outside) moved into a
    subsection of technique ART_TECHNIQUE[family] at the same LOD with the same UV
    transform; the other triangles keep theirs. Only textured trims of the main
    atlases at `lods` (None: all). Unchanged sections are returned as they are.
    Report: per section/technique, the triangles moved and their area (m2)."""
    import numpy as np

    images = {}
    # (section index, subsection index) -> (family, [triangle index], [triangle])
    found: dict = {}
    for si, section in enumerate(sections):
        for ui, sub in enumerate(section.subsections):
            if lods is not None and sub.lod not in lods:
                continue
            m = fm.resolve(section, sub)
            if m.role != fm.TRIM or m.kind != "textured" or m.family not in ART_TECHNIQUE:
                continue
            if keeps_trim(sub.material):
                continue
            if m.family not in images:
                try:
                    width, height, rgba = atlases.get(m.family)
                except (FileNotFoundError, OSError):
                    images[m.family] = None
                else:
                    images[m.family] = np.frombuffer(bytes(rgba), dtype=np.uint8).reshape(height, width, 4)
            image = images[m.family]
            if image is None:
                continue
            height, width = image.shape[:2]
            pool = section.vertices_for(sub)
            for k, tri in enumerate(sub.triangles()):
                pts = [((u := m.uv(pool[i].uv0))[0] * width, u[1] * height) for i in tri]
                if is_artwork(triangle_texels(image, pts)):
                    entry = found.setdefault((si, ui), (m.family, [], []))
                    entry[1].append(k)
                    entry[2].append([pool[i].position for i in tri])
    if not found:
        return list(sections), {}
    moved: dict = {}  # (si, ui) -> set of triangle indices
    for lod in sorted({sections[si].subsections[ui].lod for si, ui in found}):
        keys = [key for key in found if sections[key[0]].subsections[key[1]].lod == lod]
        candidates = np.array([t for key in keys for t in found[key][2]], dtype=np.float64).reshape(-1, 3, 3)
        seen = seen_from_outside(_lod_triangles(sections, lod), candidates)
        n = 0
        for key in keys:
            count = len(found[key][1])
            picked = {k for k, s in zip(found[key][1], seen[n : n + count]) if s}
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
            keep, art, area = [], [], 0.0
            pool = section.vertices_for(sub)
            for k, tri in enumerate(sub.triangles()):
                if k not in picked:
                    keep += tri
                    continue
                art += tri
                p = np.array([pool[i].position for i in tri])
                area += 0.5 * float(np.linalg.norm(np.cross(p[1] - p[0], p[2] - p[0])))
            if keep:
                subs.append(dataclasses.replace(sub, index_type=4, indices=keep))
            family = found[(si, ui)][0]
            subs.append(carbin.SubSection(ART_TECHNIQUE[family], sub.lod, 4, sub.uv_transform, art))
            stats = report.setdefault(f"{section.name}/{sub.material}", {"triangles": 0, "area": 0.0})
            stats["triangles"] += len(art) // 3
            stats["area"] += area
        out.append(dataclasses.replace(section, subsections=subs))
    return out, report
