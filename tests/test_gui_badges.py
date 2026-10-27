"""HUD badges, maker list logos and paint shading of the garage pictures
(research/gui-assets.md: "Unlimited badges and plates", "Maker list logos",
"Tone")."""

from __future__ import annotations

import re

import numpy as np

from dsfforza import REPO
from dsfforza.game import car_render as cr
from dsfforza.game import gui_assets as g

MOD_HUD = (REPO / "tools" / "asi" / "src" / "mod_hud.cpp").read_text(encoding="utf-8")


def _cars(*specs):
    cars = [{"media": media} for media, _make, _model in specs]
    texts = {media: {"make": make, "model": model, "family": None} for media, make, model in specs}
    return cars, texts


def test_slot_codes_are_the_plugins_and_draw_as_glyphs():
    codes = re.search(r"kSlotCodes\[\] = \{([^}]*)\}", MOD_HUD).group(1)
    assert tuple(int(c) for c in codes.split(",")) == g.HUD_SLOT_CODES
    # The narrow text layout (FUN_00726320) tabs at 9 and breaks the line at 10..13.
    assert not set(g.HUD_SLOT_CODES) & {0, 9, 10, 11, 12, 13}
    assert all(c < 0x20 for c in g.HUD_SLOT_CODES)


def test_glyph_strings_match_the_plugin():
    base = int(re.search(r"kGlyphStringBase = (\d+);", MOD_HUD).group(1))
    assert base == g.GLYPH_STRING_BASE
    assert g.glyph_strings([0x100, 0x2FB]) == {base + 0x100: "Ā", base + 0x2FB: "˻"}
    # Not in the range of the stock strings or of the new name strings.
    assert g.GLYPH_STRING_BASE > g.FIRST_STRING_ID + 50000


def test_maker_without_a_logo_gets_a_text_badge():
    cars, texts = _cars(("BUS_TOUR_12", "Bus", "Tour Bus"), ("ZZZ_A_01", "Zed", "A"), ("ZZZ_B_01", "Zed", "B"))
    logo = g.Image(40, 40, fill=(200, 0, 0, 255))

    def logos(make):
        return logo if make.upper() == "ZED" else None

    entries, glyphs, notes = g.plan_wide_glyphs(cars, texts, {}, {}, logos)
    assert notes["no_logo"] == ["BUS"]
    bus = entries["BUS_TOUR_12"]["ManufacturerGlyph"]
    zed = entries["ZZZ_A_01"]["ManufacturerGlyph"]
    assert entries["ZZZ_B_01"]["ManufacturerGlyph"] == zed != bus
    assert glyphs[bus].kind == "badge" and glyphs[bus].w > g.BADGE_SIZE >= glyphs[bus].h
    assert (glyphs[zed].w, glyphs[zed].h) == (g.BADGE_SIZE, g.BADGE_SIZE)

    # The one-byte plan too: no car keeps the host's badge.
    one, _, notes1 = g.plan_glyphs(cars, texts, {}, {}, [212, 213, 245, 252, 244], logos)
    assert notes1["no_logo"] == ["BUS"]
    assert all("Manufacturer" in e for e in one.values())


def test_every_car_gets_a_badge_and_plate_code_even_without_free_codes():
    """A car without Manufacturer/Model in hud.lua would get register.lua's fallback;
    with no code left for a blank glyph, the blank is BLANK_CODE (" " draws nothing)."""
    cars, texts = _cars(("ZZZ_A_01", "Zed", "A"), ("YYY_B_01", "Why", "B"))
    for free in ([], [212]):
        entries, glyphs, notes = g.plan_glyphs(cars, texts, {}, {}, list(free), lambda make: None)
        for e in entries.values():
            assert "Manufacturer" in e and "Model" in e, (free, e)
        assert {notes["blank_badge_code"], notes["blank_plate_code"]} == {g.BLANK_CODE, *free}
        assert g.BLANK_CODE not in glyphs
    assert g.BLANK_CODE == 32 and g.BLANK_CODE not in g.FREE_CODES + g.SPARE_CODES + g.UNTESTED_CODES
    lua = (REPO / "tools" / "game" / "lua" / "dsfforza" / "register.lua").read_text(encoding="utf-8")
    assert int(re.search(r"local BLANK_GLYPH = (\d+)", lua).group(1)) == g.BLANK_CODE


def test_paint_mixes_by_view_angle():
    p = cr.Paint((0.08, 0.0, 0.0), (0.2, 0.02, 0.05), power=10.0, bias=0.01)
    facing, grazing = p.at(np.array([1.0, 0.0]))
    assert np.allclose(facing, np.array(p.c2) + 0.01 * (np.array(p.c3) - np.array(p.c2)))
    assert np.allclose(grazing, p.c3)
    flat = cr.Paint.flat((128, 128, 128))
    assert np.allclose(flat.at(np.array([0.3, 0.9])), flat.c2)


def test_forza_ui_without_the_fh1_disc():
    """A build from FM4 discs only (install.ps1 -Iso <FM4 disc>): the GUI step looked for
    assets/Forza.Horizon.XBOX360.iso by name and failed, so the cars got no hud.lua and
    showed another car's nameplate (issue #98). Without the discs: no logos, text badges."""
    ui = g.ForzaUI.of([])
    assert ui.logo("Ford") is None and ui.thumbnail(1) is None
    ui.close()
    ui = g.ForzaUI(None, REPO / "no such.iso")
    assert ui.logo("Renault") is None
    ui.close()
