"""FM4 DLC cars in the build (issue #457): the merged FM4 gamedb (the disc's plus each pack's
media/db/patch/<N>_merge.slt, appended as FM4's INSERT INTO ... SELECT * FROM filedb does), the
gamedb each car is built from, the packs' cars in port_batch's requests, the opt-out, the
names from the packs and the installer's preflight. Tiny synthetic packages (tests/test_dlc.py);
no game data is read."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path

from tests.test_dlc import _assets

from dsfforza.build import fm4_dlc, forza_roster, install, port_batch


def _disc_db(path: Path) -> Path:
    """A disc gamedb: Data_Car with an INTEGER PRIMARY KEY (as FM4's disc tables have) and one
    disc car, List_FamilyModel with family 7."""
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE [Data_Car] ([Id] INTEGER PRIMARY KEY, [Year] INT, [MakeID] INT, [DisplayName] TEXT, "
        "[ModelShort] TEXT, [MediaName] TEXT, [FamilyModelID] INT)"
    )
    db.execute("CREATE TABLE [List_FamilyModel] ([Id] INTEGER PRIMARY KEY, [DisplayName] TEXT)")
    db.execute("INSERT INTO Data_Car VALUES (1, 2010, 2, 'disc car', 'Disc', 'DSC_Car_10', 7)")
    db.execute("INSERT INTO List_FamilyModel VALUES (7, 'Family Seven')")
    db.commit()
    db.close()
    return path


def _patch(rows: list[tuple], *, extra: bool = False) -> bytes:
    """A pack patch without keys (as the packs' are), its Data_Car columns in another order."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE Data_Car (MediaName, Id, Year, MakeID, DisplayName, ModelShort, FamilyModelID)")
    db.executemany("INSERT INTO Data_Car VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    if extra:
        db.execute("CREATE TABLE Pack_Only (Id, Text)")
        db.execute("INSERT INTO Pack_Only VALUES (1, 'new table')")
    data = db.serialize()
    db.close()
    return data


@contextmanager
def _packs(found):
    """fm4_dlc.packages() giving `found` (the packs of a test assets/ folder)."""
    real = fm4_dlc.packages
    fm4_dlc.packages = lambda _root=None: list(found) if fm4_dlc.enabled() else []
    try:
        yield
    finally:
        fm4_dlc.packages = real


def test_merge_appends_the_patches_and_keeps_every_disc_row():
    with tempfile.TemporaryDirectory() as tmp:
        disc = _disc_db(Path(tmp) / "disc.slt")
        later = _patch([("POR_959_87", 269, 1987, 39, "959", "959", 7)], extra=True)
        # an Id the disc has, with other values: the disc's row stays (the first one)
        clash = _patch([("OTHER_Car", 1, 1999, 3, "other", "other", 0), ("NEW_Car_11", 270, 2011, 3, "n", "n", 0)])
        target = Path(tmp) / "merged" / "gamedb.slt"
        counts = fm4_dlc.merge(disc, [(34000, "b_merge.slt", later), (23000, "a_merge.slt", clash)], target)
        assert counts == {"a_merge.slt": 1, "b_merge.slt": 2}  # in N order; the clashing row isn't added
        db = sqlite3.connect(target)
        rows = dict(db.execute("SELECT Id, MediaName FROM Data_Car"))
        assert rows == {1: "DSC_Car_10", 269: "POR_959_87", 270: "NEW_Car_11"}
        # by column name, though the patch has them in another order
        assert db.execute("SELECT Year, FamilyModelID FROM Data_Car WHERE Id = 269").fetchone() == (1987, 7)
        assert db.execute("SELECT Text FROM Pack_Only").fetchone() == ("new table",)
        db.execute("ATTACH DATABASE ? AS d", (str(disc),))
        for table in ("Data_Car", "List_FamilyModel"):
            assert (
                db.execute(f"SELECT count(*) FROM (SELECT * FROM d.{table} EXCEPT SELECT * FROM {table})").fetchone()[0]
                == 0
            )
        db.close()
        assert disc.read_bytes() == _disc_db(Path(tmp) / "again.slt").read_bytes()  # the disc file untouched


def test_merged_gamedb_is_written_once_per_set_of_packs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        packs = forza_roster.unique_packages(forza_roster.dlc_packages(_assets(root)))
        cache = root / "cache"
        disc = _disc_db(cache / "fm4" / "gamedb.slt")
        before = disc.read_bytes()
        merged = fm4_dlc.merged_gamedb([], cache, packs, log=lambda _: None)
        assert merged.name == "gamedb.slt" and merged.parent.name.startswith("dlc-")
        assert merged.parent.parent == cache / "fm4" and disc.read_bytes() == before
        db = sqlite3.connect(merged)
        assert {m for (m,) in db.execute("SELECT MediaName FROM Data_Car")} == {"DSC_Car_10", "POR_959_87"}
        db.close()
        stamp = merged.stat().st_mtime_ns
        assert fm4_dlc.merged_gamedb([], cache, packs, log=lambda _: None) == merged
        assert merged.stat().st_mtime_ns == stamp  # not written again
        # another set of packs: another file
        cars_only = [p for p in packs if p.name == "Test Car Pack"]
        assert fm4_dlc.merged_gamedb([], cache, cars_only, log=lambda _: None) != merged
        assert fm4_dlc.merged_gamedb([], cache, [], log=lambda _: None) is None


def test_a_disc_car_is_built_from_the_disc_gamedb_and_a_dlc_car_from_the_merged_one():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        packs = forza_roster.unique_packages(forza_roster.dlc_packages(_assets(root)))
        cache = root / "cache"
        disc = _disc_db(cache / "fm4" / "gamedb.slt")
        cars, _ = forza_roster.roster([], packs)
        (dlc,) = cars["por_959_87"].sources
        on_disc = forza_roster.Source("FM4", "FM4 disc 1", "Media/cars/DSC_Car_10.zip", 0, 0, "car", "fm4.iso")
        with _packs(packs):
            merged = fm4_dlc.for_source(dlc, [], cache)
            assert merged != disc and merged.parent.name.startswith("dlc-")
            assert fm4_dlc.for_source(on_disc, [], cache) == disc
            assert fm4_dlc.gamedb([], "FM4", cache) == merged  # lookups by media name
            assert fm4_dlc.existing(cache) == merged
            assert fm4_dlc.beside(cache / "fm4" / "POR_959_87") == merged
            assert fm4_dlc.beside(cache / "fh1" / "BMW_M3E30_91") == cache / "fh1" / "gamedb.slt"


def test_no_fm4_dlc_leaves_the_packs_out():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        packs = forza_roster.unique_packages(forza_roster.dlc_packages(_assets(root)))
        cache = root / "cache"
        disc = _disc_db(cache / "fm4" / "gamedb.slt")
        old = os.environ.get(fm4_dlc.ENV)
        try:
            os.environ.pop(fm4_dlc.ENV, None)
            assert fm4_dlc.enabled()
            fm4_dlc.disable()
            assert not fm4_dlc.enabled() and fm4_dlc.packages(root / "assets") == []
            with _packs(packs):
                assert fm4_dlc.gamedb([], "FM4", cache) == disc
                assert fm4_dlc.existing(cache) is None
                assert fm4_dlc.car_names(["POR_959_87"]) == {}
        finally:
            if old is None:
                os.environ.pop(fm4_dlc.ENV, None)
            else:
                os.environ[fm4_dlc.ENV] = old


def test_the_dlc_list_adds_only_the_cars_of_the_packs_found():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cars, _ = forza_roster.roster([], forza_roster.dlc_packages(_assets(root)))
        disc = forza_roster.Car("DSC_Car_10")
        disc.sources.append(forza_roster.Source("FM4", "FM4 disc 1", "x.zip", 0, 0, "car", "fm4.iso"))
        cars["dsc_car_10"] = disc
        listing = root / "fm4-dlc.txt"
        listing.write_text("# pack\npor_959_87\nMIS_Sing_12  # a pack the user lacks\nDSC_Car_10\n", encoding="utf-8")
        assert port_batch.dlc_requests(cars, listing) == [("POR_959_87", None)]


def test_the_shipped_dlc_list_is_not_in_all_txt():
    dlc = {m.lower() for m, _ in port_batch.read_list(port_batch.DLC_LIST)}
    disc = {m.lower() for m, _ in port_batch.read_list(install.DEFAULT_LIST)}
    assert len(dlc) == 148 and not dlc & disc


def test_dlc_car_names_come_from_their_packs():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        packs = forza_roster.unique_packages(forza_roster.dlc_packages(_assets(root)))
        with _packs(packs):
            names = fm4_dlc.car_names(["POR_959_87", "DSC_Car_10"], isos=[], cache=root / "cache")
        assert set(names) == {"POR_959_87"}
        assert (names["POR_959_87"]["make"], names["POR_959_87"]["year"]) == ("Porsche", 1987)


def test_preflight_says_how_the_packs_are_used():
    with tempfile.TemporaryDirectory() as tmp:
        assets = _assets(Path(tmp))
        found = forza_roster.dlc_packages(assets)
        lines = install.dlc_preflight(found)
        assert lines[0].startswith("FM4 DLC packs: 2 (") and "Test Car Pack" in lines[0] and "Tracks" in lines[0]
        assert "built too" in lines[0] and "#170" not in lines[0]
        assert "left out" in install.dlc_preflight(found, "off")[0]
        assert "--list/--car" in install.dlc_preflight(found, "named")[0]
        assert any("other title" in line and "skipped" in line for line in lines[1:])
        assert install.dlc_preflight([]) == []


def test_pack_ui_finds_a_makers_logo_by_its_icon_key():
    from tests.test_dlc import _live, _lsb2, _zip

    make_strings, refs = _lsb2("List_CarMake", {"IDS_Make_97": "Austin-Healey"})
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE List_CarMake (ID, DisplayName, IconPathBase)")
    db.execute("INSERT INTO List_CarMake VALUES (97, ?, 'AUS')", (refs["IDS_Make_97"],))
    patch = db.serialize()
    db.close()
    logo = "media/UI/Textures/UI4/Logos/Manufacturer_Logos/L_MAN_AUS_Logo.xds"
    pack = _live(
        {
            "Media/DLCZips/32_pri_65.zip": _zip({"media/db/patch/32000_merge.slt": patch, logo: b"logo bytes"}),
            "Media/DLCZips/StringTables_pri_1.zip": _zip({"EN/List_CarMake.str": make_strings}),
        },
        "Logo Pack",
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "pack"
        path.write_bytes(pack)
        ui = fm4_dlc.PackUI(forza_roster.unique_packages(forza_roster.dlc_packages(path)))
        assert ui.keys == {"austin-healey": "AUS"}
        assert ui.read("UI4/Logos/Manufacturer_Logos/L_MAN_AUS_Logo.xds") == b"logo bytes"
        assert ui.read("UI4/Logos/Manufacturer_Logos/L_MAN_MG_Logo.xds") is None
