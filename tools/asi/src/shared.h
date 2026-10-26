// State one module publishes for another. Each function has a definition in
// exactly one module and a safe answer when that module is off.
#pragma once

// mod_garage: the number of car cards the vehicles screen's car list fills at
// most for the group it shows (stock 20; 19 while a group of 20 or more cars
// is shown through a window, which keeps card 20's Header_Sprite_20 free).
int garage_card_limit();

// mod_hud: the maker list text for model `id`'s badge: "ID:n", n the localised
// one-character string of its 16-bit ManufacturerGlyph (gui_assets.py puts it
// in global.fchunk), or nullptr (no glyph, no such string, or [hud] off).
const char* hud_maker_logo_text(int id);
