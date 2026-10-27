"""The Forza disc images are told apart by their content, never by their file names
(issue #113): forza_roster.identify and the steps that need "the FH1 disc" or "FM4 disc
1/2" (string tables, garage pictures and badges, engine sound, gamedb), with only FM4,
only FH1, and odd names. The images are tiny XDVDFS images written here (a default.xex
with the title ID and disc number, a few small zips); no game data is read."""

from __future__ import annotations

import io
import os
import struct
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

from dsfforza.build import forza_roster, port_car
from dsfforza.convert import engine_sound
from dsfforza.formats import forza_strings, xiso
from dsfforza.game import gui_assets as g

FH1, FM4 = 0x4D5309C9, 0x4D530910


def _xex(title: int, disc: int = 1, discs: int = 1) -> bytes:
    """A default.xex header with one optional header: the execution info."""
    head = bytearray(0x40)
    head[:4] = b"XEX2"
    struct.pack_into(">I", head, 0x14, 1)
    struct.pack_into(">II", head, 0x18, forza_roster.XEX_EXECUTION_INFO, 0x20)
    struct.pack_into(">I", head, 0x20 + 12, title)
    head[0x20 + 18], head[0x20 + 19] = disc, discs
    return bytes(head)


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _xiso(files: dict[str, bytes]) -> bytes:
    """A rebuilt-xiso image (partition at 0) holding `files` ("a/b/c.zip": bytes, any depth)."""
    sector = xiso.SECTOR
    image = bytearray(0x10000 + sector)
    next_sector = 0x10000 // sector + 1

    def place(blob: bytes) -> int:
        nonlocal next_sector
        start = next_sector
        end = start * sector + len(blob)
        image.extend(b"\0" * max(0, end - len(image)))
        image[start * sector : end] = blob
        next_sector += -(-len(blob) // sector) or 1
        return start

    def directory(tree: dict) -> tuple[int, int]:
        rows = []
        for name, node in tree.items():
            if isinstance(node, dict):
                rows.append((name, *directory(node), xiso.DIRECTORY))
            else:
                rows.append((name, place(node), len(node), 0x80))
        table = bytearray()
        for i, (name, sec, size, attr) in enumerate(rows):  # chained through the right pointers
            raw = struct.pack("<IIBB", sec, size, attr, len(name)) + name.encode()
            raw += b"\xff" * (-(len(raw) + 4) % 4)
            right = (len(table) + 4 + len(raw)) // 4 if i + 1 < len(rows) else 0
            table += struct.pack("<HH", 0, right) + raw
        return place(bytes(table)), len(table)

    tree: dict = {}
    for path, blob in files.items():
        *folders, name = path.split("/")
        node = tree
        for f in folders:
            node = node.setdefault(f, {})
        node[name] = blob
    root_sector, root_size = directory(tree)
    image[0x10000 : 0x10000 + 28] = xiso.MAGIC + struct.pack("<II", root_sector, root_size)
    image.extend(b"\0" * (-len(image) % sector))
    return bytes(image)


def _disc(path: Path, title: int, disc: int = 1, discs: int = 1, files: dict | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_xiso({"default.xex": _xex(title, disc, discs), **(files or {})}))
    return path


def _ui_zip(game: str) -> bytes:
    """A Textures.zip with what ForzaUI reads, the "pixels" naming the game."""
    names = ["UI4/Common/Forza_Logo3.xds", "UI4/Logos/Manufacturer_Logos_LG/L_MAN_Ford_Logo.xds"]
    if game == "FM4":
        names.append("UI4/Logos/Manufacturer_Logos_LG/L_MAN_Renault_Logo.xds")  # FH1 has no Renault
    else:
        names.append("Thumbnails/Thumbnail_7.xds")
    return _zip({n: game.encode() for n in names})


def _discs(root: Path, games=("FH1", "FM4"), names: dict | None = None) -> dict[str, Path]:
    """Fake discs under odd names: FM4's disc 1 is called like FH1's (the player's
    workaround), FH1's like FM4's."""
    names = names or {
        "FH1": "Forza 4 [NTSCU][DVD1].iso",
        "FM4 disc 1": "Forza.Horizon.XBOX360.iso",
        "FM4 disc 2": "disc two (copy).ISO",
    }
    out = {}
    if "FH1" in games:
        out["FH1"] = _disc(
            root / "x" / names["FH1"],
            FH1,
            files={
                "media/ui/Textures.zip": _ui_zip("FH1"),
                "media/stringtables/EN.zip": _zip({"Data_Car.str": b"fh1 cars"}),
                "media/db/gamedb.slt": b"fh1 db",
            },
        )
    if "FM4" in games:
        out["FM4 disc 1"] = _disc(
            root / "y" / names["FM4 disc 1"],
            FM4,
            1,
            2,
            files={
                "Media/UI/Textures.zip": _ui_zip("FM4"),
                "Media/StringTables/EN.zip": _zip({"Data_Car.str": b"fm4 cars", "List_CarMake.str": b"fm4 makes"}),
                "Media/db/gamedb.slt": b"fm4 db",
            },
        )
        out["FM4 disc 2"] = _disc(root / names["FM4 disc 2"], FM4, 2, 2)
    return out


def _tmp():
    return tempfile.TemporaryDirectory(prefix="dsff-discs-")


# --- identification -------------------------------------------------------------------------------


def test_identify_reads_the_game_and_disc_whatever_the_file_is_called():
    with _tmp() as tmp:
        root = Path(tmp)
        d = _discs(root)
        junk = root / "Forza.Horizon.XBOX360 (2).iso"
        junk.write_bytes(b"not a disc" * 100)
        other = _disc(root / "Forza Horizon.iso", 0x4D5307E6)  # another game's disc
        bare = root / "bare.iso"
        bare.write_bytes(_xiso({"media/readme.txt": b"x"}))
        isos = [junk, d["FM4 disc 2"], other, d["FH1"], bare, d["FM4 disc 1"], root / "gone.iso"]
        found = {Path(x.path).name: x for x in forza_roster.identify_all(isos)}
        assert found["Forza.Horizon.XBOX360.iso"].where == "FM4 disc 1"
        assert found["Forza 4 [NTSCU][DVD1].iso"].where == "FH1"
        assert found["disc two (copy).ISO"].where == "FM4 disc 2"
        assert found[junk.name].game is None and "not an Xbox 360 disc image" in found[junk.name].where
        assert found[other.name].game is None and "4D5307E6" in found[other.name].error
        assert found["bare.iso"].game is None and "default.xex" in found["bare.iso"].error
        assert found["gone.iso"].game is None and "can't read it" in found["gone.iso"].error
        # The tools' lookups: by content, disc 1 first, whatever the order given.
        assert forza_roster.game_discs(isos) == {"FM4 disc 2": d["FM4 disc 2"], "FH1": d["FH1"], "FM4": d["FM4 disc 1"]}
        assert forza_roster.discs_of(isos, "fm4") == [d["FM4 disc 1"], d["FM4 disc 2"]]
        assert forza_roster.discs_of(isos, "FH1") == [d["FH1"]]
        assert engine_sound.isos_for("fh1", isos) == [d["FH1"]]
        assert engine_sound.isos_for("fm4", list(reversed(isos))) == [d["FM4 disc 1"], d["FM4 disc 2"]]
        found, notes = forza_roster.disc_summary(forza_roster.identify_all(isos))
        assert sorted(found) == ["FH1", "FM4 disc 1", "FM4 disc 2"]
        assert len(notes) == 4 and all("skipped" in n for n in notes), notes


def test_identify_is_cached_by_size_and_time_and_notes_what_is_missing():
    with _tmp() as tmp:
        root = Path(tmp)
        iso = _disc(root / "a.iso", FM4, 1, 2)
        assert forza_roster.identify(iso).where == "FM4 disc 1"
        with mock.patch.object(forza_roster, "XIso", side_effect=AssertionError("read again")):
            assert forza_roster.identify(iso).where == "FM4 disc 1"  # from the cache
        _disc(iso, FH1)  # another disc under the same name
        os.utime(iso, ns=(1, 1))
        assert forza_roster.identify(iso).where == "FH1"
        _, notes = forza_roster.disc_summary(forza_roster.identify_all([_disc(root / "b.iso", FM4, 2, 2)]))
        assert notes == ["no FH1 disc: its cars are skipped", "no FM4 disc 1: its cars are skipped"]
        again = _disc(root / "c.iso", FM4, 2, 2)
        found, notes = forza_roster.disc_summary(forza_roster.identify_all([root / "b.iso", again, root / "b.iso"]))
        assert found == ["FM4 disc 2"] and any("c.iso is FM4 disc 2 again" in n for n in notes)


def test_roster_skips_an_image_that_is_not_a_forza_disc():
    with _tmp() as tmp:
        root = Path(tmp)
        car = _zip({"NIS_370Z_10.carbin": b"c"})
        fm4 = _disc(root / "Forza.Horizon.XBOX360.iso", FM4, 1, 2, {"media/cars/NIS_370Z_10.zip": car})
        junk = root / "junk.iso"
        junk.write_bytes(b"\0" * 4096)
        cars, notes = forza_roster.roster([junk, fm4])
        assert cars["nis_370z_10"].sources[0].where == "FM4 disc 1" and cars["nis_370z_10"].games == "FM4"
        assert notes[0].startswith("junk.iso: not a Forza disc")


# --- the steps that read one game's disc ----------------------------------------------------------


def test_gamedb_and_string_tables_come_from_the_disc_of_that_game():
    with _tmp() as tmp:
        root = Path(tmp)
        d = _discs(root)
        isos = list(d.values())
        assert port_car.extract_gamedb(isos, "FM4", root / "cache").read_bytes() == b"fm4 db"
        assert port_car.extract_gamedb(isos, "FH1", root / "cache").read_bytes() == b"fh1 db"
        only_fm4 = [d["FM4 disc 2"], d["FM4 disc 1"]]
        assert port_car.extract_gamedb(only_fm4, "FH1", root / "cache2") is None
        # FM4 disc 1 named like the FH1 disc is FM4's, and not FH1's.
        forza_strings.extract("fm4", root / "s4", d["FM4 disc 1"])
        assert (root / "s4" / "List_CarMake.str").read_bytes() == b"fm4 makes"
        forza_strings.extract("fh1", root / "s1", d["FH1"])
        assert (root / "s1" / "Data_Car.str").read_bytes() == b"fh1 cars"
        for game, iso in (("fh1", d["FM4 disc 1"]), ("fm4", d["FM4 disc 2"]), ("fm4", d["FH1"])):
            try:
                forza_strings.extract(game, root / "bad", iso)
                raise AssertionError(f"{iso.name} taken for {game}")
            except LookupError as error:
                assert "not the" in str(error)
        # Without a disc of the game among assets/*.iso: LookupError (car_texts skips it).
        with mock.patch.object(forza_roster, "default_images", return_value=only_fm4):
            assert forza_strings.game_disc("fm4") == d["FM4 disc 1"]
            try:
                forza_strings.game_disc("fh1")
                raise AssertionError("no FH1 disc")
            except LookupError:
                pass


def _ui(isos):
    with mock.patch.object(g, "read_xds_rgba", side_effect=lambda data: (1, 1, bytearray(data))):
        ui = g.ForzaUI.of(isos)
        try:
            return {
                "ford": ui.logo("Ford"),
                "renault": ui.logo("Renault"),
                "thumbnail": ui.thumbnail(7),
                "emblem": ui.read("UI4/Common/forza_logo3.xds") or ui._read_fallback("UI4/Common/forza_logo3.xds"),
            }
        finally:
            ui.close()


def test_garage_logos_and_emblem_with_only_fm4_only_fh1_and_odd_names():
    with _tmp() as tmp:
        root = Path(tmp)
        both = _discs(root / "both")
        seen = _ui(list(both.values()))
        assert seen["ford"][2] == b"FH1" and seen["renault"][2] == b"FM4" and seen["thumbnail"][2] == b"FH1"
        assert seen["emblem"][2] == b"FH1"
        # The player's case: FM4 only, disc 1 called like the FH1 disc. FM4's logos and emblem.
        fm4 = _discs(root / "fm4", games=("FM4",))
        seen = _ui(list(fm4.values()))
        assert seen["ford"][2] == b"FM4" and seen["renault"][2] == b"FM4" and seen["emblem"][2] == b"FM4"
        assert seen["thumbnail"] is None
        fh1 = _discs(root / "fh1", games=("FH1",))
        seen = _ui(list(fh1.values()))
        assert seen["ford"][2] == b"FH1" and seen["renault"] is None and seen["emblem"][2] == b"FH1"
        none = _ui([])
        assert none == {"ford": None, "renault": None, "thumbnail": None, "emblem": None}


def test_car_names_are_read_from_each_cars_own_game_disc():
    with _tmp() as tmp:
        root = Path(tmp)
        fm4 = _discs(root, games=("FM4",))
        calls = []

        def names(game, media, gamedb=None, iso=None):
            calls.append((game, iso))
            if game == "fh1":
                raise LookupError("no FH1 disc")  # an FM4-only player: not a problem
            return {m: {"display": "Fiesta", "make": "Ford", "family": "Fiesta", "year": 2011} for m in media}

        cars = [{"media": "FOR_Fiesta_11", "game": "FM4"}, {"media": "FOR_Ka_11", "game": "FH1"}]
        with mock.patch.object(forza_strings, "car_names", side_effect=names):
            texts = g.car_texts(cars, list(fm4.values()))
        assert ("fm4", fm4["FM4 disc 1"]) in calls and ("fh1", None) in calls
        assert texts["FOR_Fiesta_11"]["model"] == "FIESTA" and texts["FOR_Ka_11"]["make"] == "FORD"
