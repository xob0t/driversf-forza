"""The car packages' garage pictures, HUD badges and plates, and names in game: generated
copies of gui.sp, Fonts.fchunk and each language's global.fchunk, which dsfforza.asi opens
in place of the game's ([archive] redirect_list; research/car-packages.md, "Manager, part 2").

The car manager (car_manager.py) calls generate() with the enabled packages. Each copy is
the game's current file (vanilla, or a merged install's) with the packages' parts appended
by gui_assets' own writers (add_pictures, add_glyphs, add_strings), so the game's files are
never written. What a car gets is what gui_assets.build gives a ported car with
--wide-glyphs, from the package's images instead of the discs:

- a maker DSF has (gui_assets.vanilla_codes on the vanilla vehicleStats): its vanilla badge
  (Manufacturer, ManufacturerName); else a 16-bit badge glyph from badge.png
  (ManufacturerGlyph, one per maker group) and a ManufacturerName string;
- a model DSF has under that maker: its vanilla plate (Model, ModelName); else a 16-bit
  plate glyph from plate.png (ModelGlyph, one per text) and a ModelName string;
- the garage picture from picture.dds (Picture1 "dsfforza_<media>", the name register.lua
  checks a hud entry against);
- GarageEmblem = 1 when the car's package carries the garage card emblem (emblem.dds,
  dsfcar.EMBLEM) and the generated Fonts.fchunk holds that texture (gui_assets.FORZA_LOGO_UID):
  the game's font already has it (a merged install's), or the packages' emblem goes into the
  copy as gui_assets.build puts it there. dsfforza.asi [hud] card_emblem binds one texture for
  that UID, so one emblem goes in: the one most enabled packages carry (every package of this
  pipeline carries the same, the Forza logo from the discs); a car whose package carries another keeps
  the SF disc. dsfforza.asi then shows the emblem on the car's garage card instead of the SF disc
  (research/gui-assets.md, "Garage card emblem").

Codes and string IDs are taken where the game's current files have none: 16-bit codes from
WIDE_FIRST (256) up past every code the vehicles_font has; strings from FIRST_STRING_ID
(700000) up, reusing an ID that already has the same text (so cars of one maker share the
ManufacturerName a merged install gave it, and with it the garage group); the maker list's
one-character badge strings at GLYPH_STRING_BASE + code (mod_hud.cpp kGlyphStringBase).
"""

from __future__ import annotations

from pathlib import Path

from dsfforza.formats import chnk

GENERATED = Path("dsfforza") / "generated"
REDIRECT = "redirect.txt"
HUD_LUA = Path("Common") / "UserLuaScripts" / "dsfforza" / "packages_hud.lua"
GUI = Path("Install") / "gui.sp"
FONTS = Path("media") / "Fonts.fchunk"
LOCALE = Path("Locale")


def _gui():
    from dsfforza.game import gui_assets

    return gui_assets


def languages(game: Path) -> list[str]:
    root = game / LOCALE
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if (p / "global.fchunk").exists())


def font_state(fonts: Path) -> tuple[set[int], bool]:
    """(the vehicles_font's glyph codes, whether the font holds an emblem texture: the
    texture UID dsfforza.asi [hud] card_emblem binds, gui_assets.FORZA_LOGO_UID)."""
    g = _gui()
    from dsfforza.formats.vehicle_mesh import child, payload

    with fonts.open("rb") as stream:
        root = chnk.read_chunk(stream, 0)
        _, pk = g.font_package(stream, root)
        codes = set(g.read_glyphs(payload(stream, child(child(pk, "NONR"), "FNTD"))))
        drsp = child(child(pk, "MDXN"), "DRSP")
        uids = {payload(stream, child(t, "UIDC"))[:8] for t in drsp.children if t.tag == "TEXC"}
    return codes, g.FORZA_LOGO_UID in uids


class Strings:
    """New string IDs, reusing one that already has the same text in our range."""

    def __init__(self, existing: dict[int, str], used: set[int]):
        g = _gui()
        self.first, self.limit = g.FIRST_STRING_ID, g.GLYPH_STRING_BASE
        self.by_text = {t: i for i, t in sorted(existing.items(), reverse=True) if self.first <= i < self.limit}
        self.used = set(used)
        self.new: dict[int, str] = {}
        self.next = self.first

    def id_of(self, text: str) -> str:
        if text not in self.by_text:
            while self.next in self.used:
                self.next += 1
            if self.next >= self.limit:
                raise SystemExit(f"no free string ID below {self.limit}")
            self.by_text[text] = self.next
            self.used.add(self.next)
            self.new[self.next] = text
        return f"ID:{self.by_text[text]}"


def _image(data: bytes, kind: str):
    """A glyph image; `kind` places it in the cell as gui_assets.glyph_y does (a badge on
    the cell's bottom, a plate centred on PLATE_CENTRE_Y)."""
    from dsfforza.formats import xds

    w, h, px = xds.png_rgba(data)
    image = _gui().Image(w, h, px)
    image.kind = kind
    return image


def plan(cars: list[dict], strings: dict[int, str], used_codes: set[int], used_strings: set[int], *, emblem: set[int]):
    """({id: hud.lua entry}, {code: glyph Image}, {string ID: text}, {picture name: DDS}).
    `emblem`: the IDs whose garage card shows the emblem (GarageEmblem)."""
    g = _gui()
    makes, models = g.vanilla_codes(strings)
    names = Strings(strings, used_strings)
    code = max([g.WIDE_FIRST - 1, *(c for c in used_codes if c >= g.WIDE_FIRST)]) + 1
    badge_codes: dict[str, int] = {}
    plate_codes: dict[str, int] = {}
    glyphs, pictures, hud = {}, {}, {}
    for car in sorted(cars, key=lambda c: c["id"]):
        n, folder = car["manifest"]["names"], Path(car["folder"])
        media = car["manifest"].get("source", {}).get("media") or car["key"].rsplit(".", 1)[-1]
        group, model = n["maker_group"].upper(), n["model"].upper()
        entry: dict = {}
        if group in makes:
            entry["Manufacturer"], entry["ManufacturerName"] = makes[group]
        else:
            entry["ManufacturerName"] = names.id_of(group)
            if group not in badge_codes and (folder / "badge.png").exists():
                badge_codes[group], code = code, code + 1
                glyphs[badge_codes[group]] = _image((folder / "badge.png").read_bytes(), "badge")
            if group in badge_codes:
                entry["ManufacturerGlyph"] = badge_codes[group]
        family = (n.get("family") or "").upper()
        vanilla = next((models[(group, t)] for t in (model, family) if t and (group, t) in models), None)
        if vanilla:
            entry["Model"], entry["ModelName"] = vanilla
        else:
            entry["ModelName"] = names.id_of(model)
            if model not in plate_codes and (folder / "plate.png").exists():
                plate_codes[model], code = code, code + 1
                glyphs[plate_codes[model]] = _image((folder / "plate.png").read_bytes(), "plate")
            if model in plate_codes:
                entry["ModelGlyph"] = plate_codes[model]
        if (folder / "picture.dds").exists():
            name = f"dsfforza_{media.lower()}"
            pictures[name] = (folder / "picture.dds").read_bytes()
            entry["Picture1"] = name
        if car["id"] in emblem:
            entry[g.EMBLEM_FIELD] = 1
        hud[car["id"]] = entry
    if code > 0xD800:
        raise SystemExit(f"16-bit glyph codes run into the UTF-16 surrogate range ({code:#x})")
    new_strings = dict(names.new)
    new_strings.update(g.glyph_strings(badge_codes.values()))  # the maker list's badges (mod_hud)
    return hud, glyphs, new_strings, pictures


def write_hud_lua(path: Path, hud: dict[int, dict]) -> None:
    lines = [
        "-- Written by dsff car_manager (src/dsfforza/build/car_gui.py); do not edit.",
        "-- vehicleStats overrides of the enabled car packages, as hud.lua's: pictures, badges and",
        "-- plates from dsfforza\\generated\\ (opened in place of the game's by dsfforza.asi).",
        "return {",
    ]
    for i, entry in sorted(hud.items()):
        fields = ", ".join(f"{k} = {v}" if isinstance(v, int) else f'{k} = "{v}"' for k, v in entry.items())
        lines.append(f"\t[{i}] = {{ {fields} }},")
    lines += ["}", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    temp.replace(path)


def clear(game: Path) -> None:
    """No generated files: the redirect list, the copies and packages_hud.lua go."""
    import shutil

    shutil.rmtree(game / GENERATED, ignore_errors=True)
    (game / HUD_LUA).unlink(missing_ok=True)


def emblems(cars: list[dict]) -> dict[int, bytes]:
    """{car ID: its package's garage card emblem texture}, for the cars whose package carries a
    good one (dsfcar.EMBLEM in the car's folder, as the manager installed it)."""
    from dsfforza.build import dsfcar

    out = {}
    for car in cars:
        try:
            data = (Path(car["folder"]) / dsfcar.EMBLEM).read_bytes()
        except OSError:
            continue
        if not dsfcar.emblem_problems(data):
            out[car["id"]] = data
    return out


def generate(game: Path, cars: list[dict], base: Path | None = None, log=print) -> dict:
    """Write the generated files for `cars` ([{id, key, manifest, folder}], the enabled
    packages) under <game>\\dsfforza\\generated, the redirect list and packages_hud.lua.
    Returns a report. Built in a new folder and swapped in, so a failure leaves the last
    generation in place. `base` holds the files they are made from (default the game folder:
    its gui.sp, Fonts.fchunk and Locale; another folder when the game folder is an overlay
    laid over a game, as in the VM tests). The packages' garage card emblem (module docstring)
    goes into the Fonts.fchunk copy unless the game's already has one."""
    import shutil

    g = _gui()
    game = Path(game)
    base = Path(base) if base else game
    if not cars:
        clear(game)
        return {"cars": 0}
    langs = languages(base)
    base_strings = base / LOCALE / ("English" if "English" in langs else langs[0]) / "global.fchunk" if langs else None
    strings = g.read_strings(base_strings) if base_strings else {}
    used_strings = set()
    for lang in langs:
        used_strings |= set(g.read_strings(base / LOCALE / lang / "global.fchunk"))
    codes, has_emblem = font_state(base / FONTS)
    own = emblems(cars)
    textures = {}
    if has_emblem:
        shown = set(own)
    elif own:
        common = max(set(own.values()), key=list(own.values()).count)
        textures = {g.FORZA_LOGO_UID: common}
        shown = {i for i, data in own.items() if data == common}
        if len(shown) < len(own):
            log(f"{len(own) - len(shown)} cars carry another garage card emblem: they keep the SF disc")
    else:
        shown = set()
    hud, glyphs, new_strings, pictures = plan(cars, strings, codes, used_strings, emblem=shown)
    out = game / GENERATED
    staged = out.with_name(out.name + ".new")
    shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    redirects = []
    report = {"cars": len(cars), "glyphs": len(glyphs), "strings": len(new_strings), "emblem": bool(textures)}
    try:
        if pictures:
            report["gui"] = g.add_pictures(base / GUI, staged / "gui.sp", pictures)
            redirects.append((GUI, GENERATED / "gui.sp"))
        if glyphs or textures:
            report["fonts"] = g.add_glyphs(base / FONTS, staged / "Fonts.fchunk", glyphs, textures)
            redirects.append((FONTS, GENERATED / "Fonts.fchunk"))
        if new_strings:
            for lang in langs:
                rel = LOCALE / lang / "global.fchunk"
                (staged / rel).parent.mkdir(parents=True, exist_ok=True)
                g.add_strings(base / rel, staged / rel, new_strings)
                redirects.append((rel, GENERATED / rel))
        lines = ["# written by dsff car_manager: game file|replacement (game folder relative)"]
        lines += [f"{a}|{b}".replace("/", "\\") for a, b in redirects]
        (staged / REDIRECT).write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    except BaseException:
        shutil.rmtree(staged, ignore_errors=True)
        raise
    old = out.with_name(out.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if out.exists():
        out.replace(old)
    staged.replace(out)
    shutil.rmtree(old, ignore_errors=True)
    write_hud_lua(game / HUD_LUA, hud)
    report["redirects"] = [str(a) for a, _ in redirects]
    log(
        f"generated: {len(pictures)} pictures, {len(glyphs)} glyphs, {len(new_strings)} strings "
        f"({len(langs)} languages){', the garage card emblem' if textures else ''} for {len(cars)} cars"
    )
    return report
