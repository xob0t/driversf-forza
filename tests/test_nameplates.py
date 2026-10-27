"""Nameplate stroke font (gui_assets.text_plate): every character of the
roster's model names draws, accented letters as their base letter plus the
mark (research/gui-assets.md, "Accented letters on the nameplates")."""

from __future__ import annotations

import numpy as np

from dsfforza.game import gui_assets as g

# Model names of tools/batches/all.txt with a character the font used to skip.
ROSTER_NAMES = (
    "REVENTÓN",
    "REVENTÓN ROADSTER",
    "MURCIÉLAGO LP 670-4 SV",
    "TT COUPÉ 3.2 QUATTRO",
    "135I COUPÉ",
    "JOHN COOPER WORKS COUPÉ",
    "V12 ZAGATO (VILLA D\N{RIGHT SINGLE QUOTATION MARK}ESTE)",
    "#1 PETRONAS TOM'S SC430",
    "CEE'D",
)


def test_roster_names_draw_every_character():
    for name in ROSTER_NAMES:
        assert g.missing_glyphs(name) == "", name


def test_latin_letters_draw():
    """Latin-1 Supplement and Latin Extended-A: every letter, upper-cased as
    text_plate does, is a stroke glyph or a base letter with known marks."""
    letters = [chr(c) for c in [*range(0xC0, 0x100), *range(0x100, 0x180)] if chr(c).isalpha()]
    assert [c for c in letters if g.missing_glyphs(c)] == []
    assert g.missing_glyphs("A\N{CJK UNIFIED IDEOGRAPH-4E2D}B\N{GREEK CAPITAL LETTER OMEGA}") == (
        "\N{CJK UNIFIED IDEOGRAPH-4E2D}\N{GREEK CAPITAL LETTER OMEGA}"
    )


def test_accent_is_a_mark_over_the_same_letters():
    plain, accented = g._text_plate("REVENTON", 40, 0.22), g._text_plate("REVENTÓN", 40, 0.22)
    extra = accented.h - plain.h
    assert extra > 0  # room above the letters for the acute
    cell = 40 / 9
    below = round(1.2 * cell)  # rows from here on are clear of the mark and its outline
    a = accented.array()[extra + below :, : plain.w].astype(int)
    b = plain.array()[below:].astype(int)
    assert np.abs(a - b).max() <= 1
    # The mark: opaque pixels above the letters, over the O only (7th of 8 letters).
    top = np.flatnonzero((accented.array()[:extra, :, 3] > 128).any(axis=0))
    assert len(top) and top[0] > accented.w * 0.6


def test_marks_below_and_ligatures():
    plain, cedilla = g._text_plate("CA", 40, 0.22), g._text_plate("ÇA", 40, 0.22)
    assert cedilla.h > plain.h
    assert (cedilla.array()[plain.h :, :, 3] > 128).any()
    assert g.text_plate("Ĳ").w == g.text_plate("IJ").w


def test_plate_size_of_an_accented_name():
    """The extra room is above the letters only: a one-line name the width fits keeps
    its letters' size, and a name splits into two lines exactly as without the
    accent. A two-line plate the height fits comes out about 16 % smaller."""
    for name, scale in (("REVENTÓN", 1), ("MURCIÉLAGO", 1), ("REVENTÓN ROADSTER", 0.84), ("TT COUPÉ S-LINE", 0.84)):
        ascii_name = name.replace("Ó", "O").replace("É", "E")
        a, b = g.nameplate(name), g.nameplate(ascii_name)
        assert abs(a.w - b.w * scale) <= 2 and a.h >= b.h, name
