"""render_compare: the geometry match, the ID buffer, lamps and lenses in car_render's
compare renders, and the region scores and flags (synthetic scenes, no game files).

uv run python -m tests render_compare
"""

from __future__ import annotations

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
    assert day[px, 0] > 1.5 * day[px, 1]  # red through the lens
    dark_off, _ = cr.shade(scene, f, cr.Lighting("dark_off", 0.05, 0.05, 0.0, 0), None, shadow=False)
    dark, _ = cr.shade(scene, f, cr.DARK, cr.dsf_lamp_rgb(scene.lamps, cr.DARK), shadow=False)
    assert dark[px, 0] > dark_off[px, 0] + 0.1  # the tail lamp glows (0x18: running lights, 0.2)


def test_lamp_levels():
    # over a 0x01 head lamp's brightness (2): a 0x18 tail/stop lamp's type brightness is 1
    assert cr.lamp_level(0x18, 4) == 0.1  # tail/stop with the running lights: 0.2
    assert cr.lamp_level(0x18, 2) == 0.5  # braking: 1
    assert cr.lamp_level(0x01, 4) == 0.0  # head lamp: off by day
    assert cr.lamp_level(0x01, 1) == 1.0
    assert cr.lamp_level(0x1D, 4) == 5.0  # day-lit head lamp: 10 / 2
    assert cr.lamp_level(0x02, 4) == 0.0  # high stop lamp: brakes only
    rgb = fr.forza_lamp_rgb([cr.LampInfo(0x1D, 8)], cr.DAY_LIT)
    assert np.allclose(rgb[0], np.array(cr.LAMP_COLOURS[8]) * 1.0)  # at a 0x01 lamp's level


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
    """Forza's lamp glows in the dark mood, ours doesn't."""
    h, w = 60, 80
    region = np.zeros((h, w), np.uint8)
    region[20:40, 20:60] = rc.RID[fr.REAR_LAMPS]
    dark_ref = np.zeros((h, w, 3), np.float32)
    dark_ref[25:35, 25:55, 0] = 0.8
    dark_ours = np.zeros((h, w, 3), np.float32)
    day = np.full((h, w, 3), 0.2, np.float32)
    r = _views(region, region, day, day, lit=({"dark": dark_ours}, {"dark": dark_ref}))
    g = rc.glow_scores(r, "dark")
    flags = rc.flags_for({"rear": {"day": rc.region_scores(r), "glow_dark": g}})
    assert any(f["what"] == "no_glow_dark" for f in flags), flags
    # and alike: no glow flag
    r = _views(region, region, day, day, lit=({"dark": dark_ref}, {"dark": dark_ref}))
    flags = rc.flags_for({"rear": {"day": rc.region_scores(r), "glow_dark": rc.glow_scores(r, "dark")}})
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


def test_glass_windows_are_windows():
    """FH1 windows drawn with the `glass` technique (glassF, glassR...) are windows, not lamp covers."""
    assert fr.region_of("glassF", "glass", "glass", "lights", True)[0] == fr.GLASS
    assert fr.region_of("glassR", "glass", "glass", "lights", False)[0] == fr.GLASS
    assert fr.region_of("lexanLHL", "glass", "glass", "lights", True) == (fr.FRONT_LAMPS, "cover")
