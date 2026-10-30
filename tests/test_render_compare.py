"""render_compare: the geometry match, the ID buffer, lamps and lenses in car_render's
compare renders, and the region scores and flags (synthetic scenes, no game files).

uv run python -m tests render_compare
"""

from __future__ import annotations

import dataclasses
import math

import numpy as np

from dsfforza.game import car_render as cr
from dsfforza.harness import forza_render as fr
from dsfforza.harness import render_compare as rc
from dsfforza.harness import render_match as rm


def _quad(x0, x1, y0, y1, z) -> np.ndarray:
    a, b, c, d = (x0, y0, z), (x1, y0, z), (x1, y1, z), (x0, y1, z)
    return np.array([[a, b, c], [a, c, d]], np.float32)


def _scene(quads, materials, uv=None, lamp=None, lamps=None) -> cr.Scene:
    pos = np.concatenate([q for q, _ in quads])
    nrm = np.concatenate([cr._face_normals(q) for q, _ in quads])
    return cr.Scene(
        pos,
        nrm,
        np.zeros(pos.shape[:2] + (2,), np.float32) if uv is None else uv,
        np.full(pos.shape[:2], 0.8, np.float32),
        np.concatenate([np.full(len(q), m, np.int32) for q, m in quads]),
        materials,
        np.zeros(len(pos), bool),
        lamp,
        lamps,
    )


FRONT = cr.Camera(0.0, 0.0, 2.0)  # looking at the car's nose along -z


def _frame(scene, camera=FRONT, size=(80, 60), box=None):
    view = cr._View(scene, camera, *size, box=box)
    return cr.rasterize(scene, view)


# --- render_match ------------------------------------------------------------------------------


def test_point_triangle_distance_matches_brute_force():
    rng = np.random.default_rng(1)
    tri = rng.normal(size=(200, 3, 3))
    p = rng.normal(size=(200, 3)) * 2
    d = rm.point_triangle_distance(p, tri[:, 0], tri[:, 1], tri[:, 2])
    # brute force: the nearest of many points on each triangle
    u, v = np.meshgrid(np.linspace(0, 1, 120), np.linspace(0, 1, 120))
    keep = u + v <= 1
    u, v = u[keep], v[keep]
    for i in range(0, 200, 20):
        a, b, c = tri[i]
        pts = a + np.outer(u, b - a) + np.outer(v, c - a)
        brute = np.linalg.norm(pts - p[i], axis=1).min()
        assert d[i] <= brute + 1e-9 and brute - d[i] < 0.05, (i, d[i], brute)


def test_match_finds_copies_pieces_and_none():
    """Copied triangles (positions on the 1/4096 grid), a piece cut out of one, a moved
    plate and a triangle with no source."""
    rng = np.random.default_rng(2)
    ref = rng.uniform(-1, 1, size=(400, 1, 3)) + rng.normal(scale=0.03, size=(400, 3, 3))
    ours = [np.round(ref[:300] * 4096) / 4096]  # copies, quantised as the converter writes them
    a, b, c = ref[300]
    piece = np.array([[a, (a + b) / 2, (a + c) / 2]])  # a corner piece of triangle 300
    plate = ref[301:302] + np.array([0.0, 0.0, 0.01])  # 1 cm off its face: a near match
    far = ref[302:303] + np.array([0.0, 0.0, 0.5])  # nowhere near
    ours = np.concatenate(ours + [piece, plate, far])
    src, dist = rm.match(ours, ref)
    assert (src[:300] == np.arange(300)).all()
    assert dist[:300].max() < rm.ON
    assert src[300] == 300 and dist[300] < 1e-9
    assert src[301] == 301 and rm.ON < dist[301] <= rm.NEAR
    assert src[302] == -1


def test_register_recovers_the_placement():
    rng = np.random.default_rng(3)
    forza = rng.uniform(-2, 2, size=(5000, 3))
    dy, dz = -0.1988, 0.3471
    placed = np.round(np.stack([-forza[:, 0], forza[:, 1] + dy, -forza[:, 2] + dz], 1) * 4096) / 4096
    zero = np.stack([-forza[:, 0], forza[:, 1], -forza[:, 2]], 1)
    gy, gz = rm.register(placed, zero)
    assert abs(gy - dy) < 1e-3 and abs(gz - dz) < 1e-3


# --- car_render: ID buffer, alpha test, lenses, lamps ------------------------------------------------


def test_rasterize_keeps_the_nearest_triangle():
    near = cr.Material("textured", None, np.ones(3, np.float32))
    far = cr.Material("textured", None, np.full(3, 0.5, np.float32))
    scene = _scene([(_quad(-1, 1, -1, 1, 0.5), 0), (_quad(-2, 2, -2, 2, 0.0), 1)], [near, far])
    f = _frame(scene)
    centre = f.tri.reshape(60, 80)[30, 40]
    assert centre in (0, 1)  # the near quad's triangles
    corner = f.tri.reshape(60, 80)[2, 2]
    assert corner in (2, 3, -1)


def test_alpha_test_cuts_holes():
    """An alpha-tested material with alpha 0 texels shows what is behind it."""
    tex = np.zeros((2, 2, 4), np.float32)  # alpha 0 everywhere
    cut = cr.Material("textured", tex, np.ones(3, np.float32), alpha_test=True)
    back = cr.Material("textured", None, np.full(3, 0.5, np.float32))
    scene = _scene([(_quad(-1, 1, -1, 1, 0.5), 0), (_quad(-2, 2, -2, 2, 0.0), 1)], [cut, back])
    assert set(np.unique(_frame(scene).tri)) <= {2, 3, -1}


def test_lens_tints_and_lamp_glows():
    """A red lens over a white lamp: the lamp shows red through it, and in the dark mood
    an emissive lamp glows by its texel alpha with its lamp's colour."""
    lamp_tex = np.ones((2, 2, 4), np.float32)  # white, alpha 1 (glow mask)
    lens_tex = np.zeros((2, 2, 4), np.float32)
    lens_tex[..., 0], lens_tex[..., 3] = 0.8, 0.6  # red texel, opacity 0.6
    lamp = cr.Material("textured", lamp_tex, np.ones(3, np.float32), emissive=True, shading=cr.dsf_shading("lights"))
    lens = cr.Material(
        "lens", lens_tex, np.ones(3, np.float32), lens=cr.Lens(cap=0.7), shading=cr.dsf_shading("lights")
    )
    scene = _scene(
        [(_quad(-1, 1, -1, 1, 0.0), 0), (_quad(-1, 1, -1, 1, 0.1), 1)],
        [lamp, lens],
        lamp=np.array([0, 0, -1, -1], np.int32),
        lamps=[cr.LampInfo(0x18, 6)],
    )
    f = _frame(scene)
    assert f.layers and len(f.layers[0][0])
    day, _ = cr.shade(scene, f, cr.DAY, cr.dsf_lamp_rgb(scene.lamps, cr.DAY), shadow=False)
    px = 30 * 80 + 40
    assert day[px, 0] > 1.5 * day[px, 1]  # the lens's red over the lamp
    # Glass at opacity 0.6 passes (1 - 0.6)^2 of the lamp's glow (Forza's blend, #379); a Forza
    # detail_glass_red lens filters it by its red instead.
    dark_off, _ = cr.shade(scene, f, cr.DARK, None, shadow=False)
    dark, _ = cr.shade(scene, f, cr.DARK, cr.dsf_lamp_rgb(scene.lamps, cr.DARK), shadow=False)
    bare = _scene([(_quad(-1, 1, -1, 1, 0.0), 0)], [lamp], lamp=np.array([0, 0], np.int32), lamps=scene.lamps)
    fb = _frame(bare)
    on, _ = cr.shade(bare, fb, cr.DARK, cr.dsf_lamp_rgb(bare.lamps, cr.DARK), shadow=False)
    off, _ = cr.shade(bare, fb, cr.DARK, None, shadow=False)
    glow, through = on[px, 0] - off[px, 0], dark[px, 0] - dark_off[px, 0]
    assert abs(through - 0.16 * glow) < 0.03 * glow, (through, glow)
    lens.lens = cr.Lens(technique="detail_glass_red")
    red, _ = cr.shade(scene, f, cr.DARK, cr.dsf_lamp_rgb(scene.lamps, cr.DARK), shadow=False)
    assert red[px, 0] > dark[px, 0] and red[px, 1] < 0.01  # the lamp's red through the red filter


def test_cabin_copy_behind_a_lamp_lens_loses_to_the_lamp():
    """The interior entry's copy of the rear panel in front of a lamp reflector recessed
    1.5 cm and 15 cm behind it, a lamp lens over both: the copy loses to the reflector (the
    cabin is seen through window glass, not through lamp lenses; #313's empty lamps)."""
    bowl = cr.Material("textured", None, np.ones(3, np.float32))
    copy = cr.Material("paint", None, np.full(3, 0.2, np.float32))
    lens = cr.Material("lens", None, np.ones(3, np.float32), lens=cr.Lens(cap=0.7))
    for recess in (0.015, 0.15):
        scene = _scene(
            [(_quad(-1, 1, -1, 1, 0.0), 0), (_quad(-1, 1, -1, 1, recess), 1), (_quad(-1, 1, -1, 1, 0.2), 2)],
            [bowl, copy, lens],
        )
        scene.inside = np.array([False, False, True, True, False, False])
        centre = _frame(scene).tri.reshape(60, 80)[30, 40]
        assert centre in (0, 1), recess  # the reflector, under the lens


def test_lamp_levels():
    """The colour multiplier the game gives each lamp type (research/lights-re.md)."""
    from dsfforza.convert import lights

    run, brake, head = 4, 6, 5
    assert lights.lamp_level(0x18, run) == 0.2  # tail/stop with the running lights
    assert lights.lamp_level(0x18, brake) == 1.0
    assert lights.lamp_level(0x03, run) == 2.0  # tail / position lamp
    assert lights.lamp_level(0x01, run) == 0.0  # head lamp: never lit in free roam
    assert lights.lamp_level(0x01, head) == 2.0
    assert lights.lamp_level(0x1D, run) == 1.0  # day-lit head lamp: 10 only with Headlights
    assert lights.lamp_level(0x1D, head) == 10.0
    assert lights.lamp_level(0x02, run) == 0.0  # high stop lamp: brakes only
    assert lights.lamp_level(0x14, run) == 1.0  # DRL + indicator
    rgb = cr.dsf_lamp_rgb([cr.LampInfo(0x18, lights.RED), cr.LampInfo(0x1D, lights.WHITE)], cr.DAY_LIT)
    assert np.allclose(rgb[0], (5 * 0.587755 * 0.2, 0, 0))  # as car 130's tails upload by day
    assert np.allclose(rgb[1], 5 * np.array(lights.LAMP_COLOUR[lights.WHITE]))


def test_game_tone_map():
    """The game's post chain: Reinhard with white W, then the grade; the garage tone by day."""
    assert np.allclose(cr.tonemap(np.array([[5.0, 5.0, 5.0]]), 1.0, 5.0), 1.0, atol=1e-3)  # W maps to 1
    lit = cr.display(np.array([[0.2, 0.2, 0.2], [3.0, 3.0, 3.0], [0.5, 0.02, 0.02]], np.float32), cr.DAY_LIT)
    assert 0.1 < lit[0, 0] < 0.3 and 0.8 < lit[1, 0] < 0.95
    assert lit[2, 0] > 3 * lit[2, 1] > 0.05  # a red stays red, but the grade lifts its weak channels
    assert np.allclose(cr.display(np.array([[0.5, 0.5, 0.5]]), cr.DAY), 0.5 * cr.EXPOSURE)


def test_garage_render_unchanged_by_compare_fields():
    """The garage picture's shading ignores the compare-only material fields' defaults."""
    paint = cr.Material("paint", None, np.array([0.8, 0.05, 0.05], np.float32))
    scene = _scene([(_quad(-1, 1, 0, 1.4, 2.3), 0)], [paint])
    a = cr.render(scene, cr.VANILLA_CAMERA, 120, 80)
    b = cr.render(scene, cr.VANILLA_CAMERA, 120, 80)
    assert (a == b).all() and a[..., 3].max() == 255


# --- forza_render -------------------------------------------------------------------------------


def test_region_of():
    assert fr.region_of("body", "head_light", "textured", "lights", True) == (fr.FRONT_LAMPS, "head")
    assert fr.region_of("trunk", "detail_glass_red", "textured", "lights", False) == (fr.REAR_LAMPS, "lens")
    assert fr.region_of("glassLHL", "glass", "glass", "lights", True) == (fr.FRONT_LAMPS, "cover")
    assert fr.region_of("body", "body", "paint", "nodamage", True)[0] == fr.BODY
    assert fr.region_of("bumperF", "misc", "trim", "nodamage", True)[0] == fr.TRIMS
    assert fr.region_of("trunk", "badge", "textured", "nodamage", False)[0] == fr.BADGES
    assert fr.region_of("glassF", "window", "glass", "nodamage", True)[0] == fr.GLASS
    assert fr.region_of("interior", "interior", "textured", "nodamage", True)[0] == fr.CABIN
    assert fr.region_of("body", "chrome", "chrome", "nodamage", True)[0] == fr.CHROME


# --- scores and flags ---------------------------------------------------------------------------


def _views(ours_region, ref_region, ours_rgb, ref_rgb, programs=None, lit=None):
    h, w = ours_region.shape
    r = {
        "regions": {"ours": ours_region, "ref": ref_region},
        "ours": {"day": ours_rgb},
        "ref": {"day": ref_rgb},
        "alpha": {"ours": np.ones((h, w)), "ref": np.ones((h, w))},
        "programs": programs if programs is not None else np.full((h, w), rc.PID["lights"], np.uint8),
    }
    if lit:
        r["ours"].update(lit[0])
        r["ref"].update(lit[1])
    return r


def test_painted_on_lamp_is_detail_lost():
    """A lamp drawn as one flat colour where Forza's has structure: detail lost."""
    h, w = 60, 80
    region = np.zeros((h, w), np.uint8)
    region[10:50, 10:70] = rc.RID[fr.FRONT_LAMPS]
    rng = np.random.default_rng(4)
    ref = np.full((h, w, 3), 0.05, np.float32)
    ref[10:50, 10:70] = rng.uniform(0.02, 0.6, size=(40, 60, 1))  # reflector structure
    ours = np.full((h, w, 3), 0.05, np.float32)
    ours[10:50, 10:70] = 0.2  # flat
    progs = np.full((h, w), rc.PID["trim"], np.uint8)  # and drawn by the shared trim
    m = rc.region_scores(_views(region, region, ours, ref, progs))
    lamp = m[fr.FRONT_LAMPS]
    assert lamp["missing"] == 0 and lamp["contrast_ours"] < 0.2 * lamp["contrast_ref"]
    flags = rc.flags_for({"front": {"day": m}})
    whats = {f["what"] for f in flags if f["region"] == fr.FRONT_LAMPS}
    assert {"detail", "drawn_as"} <= whats, flags


def test_missing_region_and_plate_over_badge():
    """A plate over the grille's badge (#159) flags; over a trim alone (its holder) it doesn't."""
    h, w = 60, 80
    ref_region = np.zeros((h, w), np.uint8)
    ref_region[5:55, 5:75] = rc.RID[fr.BODY]
    ref_region[30:50, 20:60] = rc.RID[fr.TRIMS]  # the grille
    ref_region[35:45, 30:50] = rc.RID[fr.BADGES]  # its badge
    ours_region = ref_region.copy()
    ours_region[30:50, 20:60] = rc.RID[fr.PLATES]  # a plate over all of it
    rgb = np.full((h, w, 3), 0.3, np.float32)
    m = rc.region_scores(_views(ours_region, ref_region, rgb, rgb))
    assert m[fr.TRIMS]["missing"] == 1.0
    flags = rc.flags_for({"front": {"day": m}})
    assert any(f["what"] == "plate_over" for f in flags), flags
    assert any(f["what"] == "missing" and f["region"] == fr.TRIMS for f in flags), flags
    ref_region[35:45, 30:50] = rc.RID[fr.TRIMS]
    m = rc.region_scores(_views(ours_region, ref_region, rgb, rgb))
    assert not any(f["what"] == "plate_over" for f in rc.flags_for({"front": {"day": m}}))


def test_no_glow_flag():
    """Forza's lamp glows with the running lights, ours doesn't."""
    h, w = 60, 80
    region = np.zeros((h, w), np.uint8)
    region[20:40, 20:60] = rc.RID[fr.REAR_LAMPS]
    base = np.full((h, w, 3), 0.2, np.float32)
    lit_ref = base.copy()
    lit_ref[25:35, 25:55, 0] += 0.8
    lit_ours = base.copy()
    day = np.full((h, w, 3), 0.2, np.float32)

    def lit(ours, ref):
        return ({"day_lit": ours, "day_lit_base": base}, {"day_lit": ref, "day_lit_base": base})

    r = _views(region, region, day, day, lit=lit(lit_ours, lit_ref))
    g = rc.glow_scores(r, "day_lit")
    flags = rc.flags_for({"rear": {"day": rc.region_scores(r), "glow_day_lit": g}})
    assert any(f["what"] == "no_glow_day_lit" for f in flags), flags
    # and alike: no glow flag
    r = _views(region, region, day, day, lit=lit(lit_ref, lit_ref))
    flags = rc.flags_for({"rear": {"day": rc.region_scores(r), "glow_day_lit": rc.glow_scores(r, "day_lit")}})
    assert not any(f["what"].startswith(("no_glow", "glow_place")) for f in flags), flags


def test_identical_renders_raise_no_flags():
    h, w = 60, 80
    region = np.zeros((h, w), np.uint8)
    region[5:55, 5:75] = rc.RID[fr.BODY]
    region[10:20, 10:30] = rc.RID[fr.FRONT_LAMPS]
    rng = np.random.default_rng(5)
    rgb = rng.uniform(0, 0.5, size=(h, w, 3)).astype(np.float32)
    m = rc.region_scores(_views(region, region, rgb, rgb))
    assert rc.flags_for({"front3q": {"day": m}}) == []


# --- rescore and the before/after digest -------------------------------------------------------


def test_stored_renders_round_trip(tmp_path=None):
    import tempfile
    from pathlib import Path

    h, w = 20, 30
    rng = np.random.default_rng(6)
    r = {
        "regions": {
            "ours": rng.integers(0, 10, (h, w)).astype(np.uint8),
            "ref": rng.integers(0, 10, (h, w)).astype(np.uint8),
        },
        "programs": rng.integers(0, 9, (h, w)).astype(np.uint8),
        "alpha": {"ours": np.ones((h, w)), "ref": np.ones((h, w))},
        "ours": {
            "day": rng.uniform(0, 2, (h, w, 3)).astype(np.float32),
            "dark": rng.uniform(0, 4, (h, w, 3)).astype(np.float32),
        },
        "ref": {
            "day": rng.uniform(0, 2, (h, w, 3)).astype(np.float32),
            "dark": rng.uniform(0, 4, (h, w, 3)).astype(np.float32),
        },
    }
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "v.npz"
        rc.save_renders(path, r)
        back = rc.load_renders(path)
    assert (back["regions"]["ours"] == r["regions"]["ours"]).all() and (back["programs"] == r["programs"]).all()
    for side in ("ours", "ref"):
        for mode in ("day", "dark"):
            assert np.abs(back[side][mode] - r[side][mode]).max() < 1 / rc.HDR_SCALE + 1e-6


def test_digest_ignores_handling():
    """Two cars whose VEHC differ only in VO3D (handling, cameras) draw alike; a changed
    vertex buffer doesn't."""
    import io

    from dsfforza.formats.chnk import Entry
    from dsfforza.harness import render_diff

    def leaf(tag, data, at):
        return Entry(tag, at, len(data), 0, 0, None)

    def car(vxdt: bytes, vo3d: bytes):
        blob = vxdt + vo3d + b"MODL"
        mdxn = Entry("MDXN", 0, 0, 0, 0, None, [leaf("VXDT", vxdt, 0)])
        nonr = Entry(
            "NONR", 0, 0, 0, 0, None, [leaf("VO3D", vo3d, len(vxdt)), leaf("MODL", b"MODL", len(vxdt) + len(vo3d))]
        )
        return io.BytesIO(blob), {0: Entry("VEHC", 0, 0, 0, 0, None, [mdxn, nonr])}

    a = render_diff.vehc_digest(*car(b"vertices", b"handling A"))
    b = render_diff.vehc_digest(*car(b"vertices", b"handling B"))
    c = render_diff.vehc_digest(*car(b"vertexes", b"handling A"))
    assert a == b != c


def test_glass_windows_are_windows():
    """FH1 windows drawn with the `glass` technique (glassF, glassR...) are windows, not lamp covers."""
    assert fr.region_of("glassF", "glass", "glass", "lights", True)[0] == fr.GLASS
    assert fr.region_of("glassR", "glass", "glass", "lights", False)[0] == fr.GLASS
    assert fr.region_of("lexanLHL", "glass", "glass", "lights", True) == (fr.FRONT_LAMPS, "cover")


def test_diff_lists_added_geometry():
    """Geometry ours adds with no Forza counterpart (region "other": a virtual display's
    housing, #249) is listed as "added" when it appears; nothing is listed when alike."""
    from dsfforza.harness import render_diff

    h, w = 60, 80
    a = np.zeros((h, w), np.uint8)
    a[10:50, 10:70] = rc.RID[fr.COCKPIT]
    b = a.copy()
    b[20:35, 30:50] = rc.RID["other"]  # a new housing on the dash
    d = np.zeros((h, w))
    changes = render_diff.region_changes(a, b, d)
    assert {c["region"] for c in changes} == {"added", fr.COCKPIT}, changes
    added = next(c for c in changes if c["region"] == "added")
    assert added["pixels_before"] == 0 and added["pixels_after"] == 300
    assert render_diff.region_changes(a, a, d) == []


def test_diff_frames_both_builds_alike():
    """A plate moved 3 cm outward changes the car's extent; framed by the union of both
    builds' extents, only the plate's pixels change (#254)."""
    from dsfforza.harness import render_diff

    paint = cr.Material("textured", None, np.full(3, 0.4, np.float32))
    plate = cr.Material("textured", None, np.full(3, 0.9, np.float32))

    def pair(z):
        body = np.concatenate([_quad(-1, 1, 0, 1, 1.0), _quad(-1, 1, 0, 1, -1.0)])
        scene = _scene([(body, 0), (_quad(-0.2, 0.2, 0.2, 0.4, z), 1)], [paint, plate])
        region = np.array([rc.RID[fr.BODY]] * 4 + [rc.RID[fr.PLATES]] * 2, np.uint8)
        pts = scene.pos.reshape(-1, 3)
        return rc.Pair(
            scene, scene, region, region, np.zeros(6, np.uint8), [], np.full(6, -1), (pts.min(0), pts.max(0)), {}
        )

    a, b = pair(1.01), pair(1.04)
    view = rc.View("front", cr.Camera(0.0, 0.0, 2.0), modes=("day",))
    box = rc.union_box(a.box, b.box)
    ra, rb = rc.render_view(a, view, (120, 80), box), rc.render_view(b, view, (120, 80), box)
    d = np.linalg.norm(rc._lab(ra["ours"]["day"]) - rc._lab(rb["ours"]["day"]), axis=-1)
    changed = {c["region"] for c in render_diff.region_changes(ra["regions"]["ours"], rb["regions"]["ours"], d)}
    assert fr.BODY not in changed, changed
    # Framed by each build's own extent, the body shifts too.
    ra, rb = rc.render_view(a, view, (120, 80)), rc.render_view(b, view, (120, 80))
    d = np.linalg.norm(rc._lab(ra["ours"]["day"]) - rc._lab(rb["ours"]["day"]), axis=-1)
    changed = {c["region"] for c in render_diff.region_changes(ra["regions"]["ours"], rb["regions"]["ours"], d)}
    assert fr.BODY in changed, changed


# --- paint (research/paint-re.md) ------------------------------------------------------------

ENTRY = {
    "g_vec4TintColour1_Alpha": (0.0, 0.0, 0.0, 1.0),
    "g_vec3TintColour2": (0.111, 0.003, 0.003, 1.0),
    "g_vec3TintColour3": (0.494, 0.117, 0.117, 1.0),
    "g_fresnelpower": 0.25,
    "g_fresnelbias": 0.01,
    "g_hCustomFresnelPower": 1.0,
    "g_hCustomReflectionAmount": 0.5,
    "g_hSecondSpecularIntensity": 0.1,
    "g_hSecondSpecularPower": 5.0,
}


def test_paint_entry_is_drawn_as_stored():
    """A low-power entry shows colour 3 almost everywhere, as the game draws it (#291): no
    picture-model floor on the power in the compare renders."""
    p = cr.Paint.entry(ENTRY)
    facing = p.at(np.array([0.9]))[0]
    assert facing[0] > 0.3  # mostly colour 3 (0.494) even facing the camera
    assert (p.reflection, p.reflection_power, p.sheen, p.sheen_power) == (0.5, 1.0, 0.1, 5.0)
    picture = cr.picture_paint(p.c2, p.c3, p.power, p.bias, False)
    assert picture.at(np.array([0.9]))[0][0] < 0.12  # the garage picture keeps its model


def _paint_terms(p, cos_v):
    scene = _scene([(_quad(-1, 1, -1, 1, 0.0), 0)], [cr.Material("paint", None, np.ones(3, np.float32))])
    view = cr._View(scene, FRONT, 80, 60)
    s = math.sqrt(1 - cos_v**2)
    n = np.array([[0.0, s, cos_v]], np.float32)
    ray = np.array([[0.0, 0.0, -1.0]], np.float32)
    refl = ray - 2 * (ray @ n[0])[:, None] * n
    light = np.array([0.0, 0.0, -1.0], np.float32)  # behind the camera's back: no lobe
    a = np.array([[0.2, 0.0, 0.0]], np.float32)
    mask = np.array([cr.PAINT_SPEC_MASK], np.float32)
    return cr._shade_paint(p, view, light, n, a, mask, np.ones(1, np.float32), refl, np.array([cos_v]), cr.DAY)


def test_paint_reflection_is_off_facing_and_scales_with_the_amount():
    """The reflection's Fresnel term is max(0.025, (1 - N.E)^cfp - 0.5): the floor facing the
    camera, rising at grazing angles; twice g_hCustomReflectionAmount, twice the reflection."""
    p = cr.Paint.entry(ENTRY)
    _, f_facing = _paint_terms(p, 0.9)
    _, f_grazing = _paint_terms(p, 0.1)
    assert abs(f_facing[0] - cr.PAINT_FRESNEL_MIN) < 1e-6
    assert abs(f_grazing[0] - 0.4) < 1e-5
    c1, _ = _paint_terms(p, 0.1)
    c2, _ = _paint_terms(dataclasses.replace(p, reflection=1.0), 0.1)
    c0, _ = _paint_terms(dataclasses.replace(p, reflection=0.0), 0.1)
    assert np.allclose(c2 - c0, 2 * (c1 - c0), atol=1e-5) and (c1 - c0).min() > 0
