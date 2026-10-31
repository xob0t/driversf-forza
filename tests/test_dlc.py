"""FM4 DLC packages as a car source (issue #162): packages found by their STFS header under any
names, their cars in the roster only when asked for, names and years from each pack's own
gamedb patch and string tables, a pack car extracted like a disc car, and the installer's
preflight lines. The packages are tiny LIVE packages written here; no game data is read."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import struct
import tempfile
import zipfile
from pathlib import Path

from dsfforza.build import forza_roster, install, port_car
from dsfforza.formats import forza_strings, stfs

FM4, FH1 = 0x4D530910, 0x4D5309C9
BLOCK = stfs.BLOCK


def _zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _lsb2(table: str, strings: dict[str, str]) -> tuple[bytes, dict[str, str]]:
    """A big-endian LSB2 string table (forza_strings.parse's layout) and the "_&N" reference
    of each string id."""
    ids = list(strings)
    hashes = [forza_strings.str_hash(i) for i in ids]
    text = b"".join(strings[i].encode("utf-16-be") + b"\0\0" for i in ids)
    offsets, at = [], 0
    for i in ids:
        offsets.append(at // 2)
        at += len(strings[i].encode("utf-16-be")) + 2
    rows = [(h, o) for h, o in zip(hashes, offsets, strict=True)] + [(hashes[0], offsets[0])]
    sec0 = struct.pack(">II", 0, len(ids)) + b"".join(struct.pack(">HI", h, o) for h, o in rows) + text
    names = b"".join(i.encode() + b"\0" for i in ids)
    id_offsets, at = [], 0
    for i in ids:
        id_offsets.append(at)
        at += len(i) + 1
    sec1 = struct.pack(">II", 0, len(ids))
    sec1 += b"".join(struct.pack(">HHI", h, 0, o) for h, o in zip(hashes, id_offsets, strict=True)) + names
    head = b"LSB2" + bytes([1, 0, 0, 0]) + struct.pack(">H", 1) + b"\0\0"
    first = len(head) + 16
    head += struct.pack(">IIII", first, 2, first, first + len(sec0))
    refs = {i: f"_&{forza_strings.str_hash(table) << 16 | h}" for i, h in zip(ids, hashes, strict=True)}
    return head + sec0 + sec1, refs


def _merge_db(cars: list[tuple], makes: list[tuple] = ()) -> bytes:
    """A gamedb patch: Data_Car (Id, Year, MakeID, DisplayName, ModelShort, MediaName,
    FamilyModelID), List_CarMake (ID, DisplayName), List_FamilyModel (Id, DisplayName)."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE Data_Car (Id, Year, MakeID, DisplayName, ModelShort, MediaName, FamilyModelID)")
    db.execute("CREATE TABLE List_CarMake (ID, DisplayName)")
    db.execute("CREATE TABLE List_FamilyModel (Id, DisplayName)")
    db.executemany("INSERT INTO Data_Car VALUES (?, ?, ?, ?, ?, ?, ?)", cars)
    db.executemany("INSERT INTO List_CarMake VALUES (?, ?)", makes)
    data = db.serialize()
    db.close()
    return data


def _live(files: dict[str, bytes], name: str, title: int = FM4, content_type: int = 2) -> bytes:
    """A female LIVE package holding `files` ("Media/DLCZips/x.zip": bytes), each in
    consecutive blocks after the one-block file table (fewer than 170 blocks: one hash table)."""
    header_size = 0xAD0E
    first = (header_size + 0xFFF) & ~0xFFF
    table = bytearray(BLOCK)
    data_blocks: list[bytes] = [bytes(BLOCK)]  # block 0: the file table, filled in below
    slots: dict[str, int] = {}

    def entry(path: str, flags: int, start: int, size: int, count: int) -> int:
        slot = len(slots) + 1  # slot 0 stays empty, as in tests/test_formats.py
        parent, leaf = path.rsplit("/", 1) if "/" in path else ("", path)
        e = bytearray(0x40)
        e[: len(leaf)] = leaf.encode()
        e[0x28] = len(leaf) | flags
        e[0x29:0x2C] = e[0x2C:0x2F] = count.to_bytes(3, "little")
        e[0x2F:0x32] = start.to_bytes(3, "little")
        struct.pack_into(">hI", e, 0x32, slots.get(parent, -1), size)
        table[slot * 0x40 : (slot + 1) * 0x40] = e
        slots[path] = slot
        return slot

    for path, blob in files.items():
        parts = path.split("/")
        for depth in range(1, len(parts)):
            folder = "/".join(parts[:depth])
            if folder not in slots:
                entry(folder, 0x80, 0, 0, 0)
        count = -(-len(blob) // BLOCK) or 1
        entry(path, 0x40, len(data_blocks), len(blob), count)
        for i in range(count):
            data_blocks.append(blob[i * BLOCK : (i + 1) * BLOCK].ljust(BLOCK, b"\0"))
    data_blocks[0] = bytes(table)
    assert len(data_blocks) <= stfs.PER_TABLE
    hashes = bytearray(BLOCK)
    for block, content in enumerate(data_blocks):
        hashes[block * 0x18 : block * 0x18 + 0x18] = hashlib.sha1(content).digest() + b"\x80" + b"\xff\xff\xff"
    pkg = bytearray(first)
    pkg[0:4] = b"LIVE"
    struct.pack_into(">II", pkg, 0x340, header_size, content_type)
    struct.pack_into(">I", pkg, 0x360, title)
    raw = name.encode("utf-16-be")
    pkg[0x411 : 0x411 + len(raw)] = raw
    vd = bytearray(0x24)
    vd[0], vd[2] = 0x24, 1  # size, flags: female
    struct.pack_into("<H", vd, 3, 1)  # file table: 1 block from block 0
    vd[8:28] = hashlib.sha1(bytes(hashes)).digest()
    struct.pack_into(">II", vd, 0x1C, len(data_blocks), 0)
    pkg[0x379 : 0x379 + 0x24] = vd
    return bytes(pkg) + bytes(hashes) + b"".join(data_blocks)


def _car_pack(name: str = "Test Car Pack") -> bytes:
    """One pack with POR_959_87 (and its wheel folder), a gamedb patch naming it, and
    EN string tables; Porsche's maker row is in the pack, the family only on the disc."""
    car_strings, car_refs = _lsb2("Data_Car", {"IDS_DisplayName_1": "Porsche 959", "IDS_ModelShort_1": "959"})
    make_strings, make_refs = _lsb2("List_CarMake", {"IDS_Make_39": "Porsche"})
    merge = _merge_db(
        [(269, 1987, 39, car_refs["IDS_DisplayName_1"], car_refs["IDS_ModelShort_1"], "POR_959_87", 7)],
        [(39, make_refs["IDS_Make_39"])],
    )
    inner = _zip(
        {
            "media/cars/POR_959_87/por_959_87.carbin": b"main carbin",
            "media/cars/POR_959_87/nodamage.xds": b"textures",
            "media/cars/POR_959_87/physics/maxdata.xml": b"<MAXData/>",
            "media/wheels/POR_959_87/wheel.xds": b"wheel texture",
            "media/db/patch/34000_merge.slt": merge,
        }
    )
    strings = _zip({"EN/Data_Car.str": car_strings, "EN/List_CarMake.str": make_strings})
    return _live(
        {
            "Media/DLCZips/34_pri_65.zip": inner,
            "Media/DLCZips/StringTables_pri_25034.zip": strings,
            "Media/DLCZips/zipmount.xml": b"<zipmount/>",
        },
        name,
    )


def _assets(root: Path) -> Path:
    """An assets/ folder laid out like the user's: one odd folder per pack, packages under
    hash names without an extension, plus things that aren't FM4 DLC."""
    assets = root / "assets"
    pack = assets / "Forza Motorsport 4 Full DLC Collection" / "[2012.05.22] Porsche Expansion Pack"
    pack.mkdir(parents=True)
    (pack / "E3470A83732B9093D220BF7D6506FB30517900064D").write_bytes(_car_pack())
    tracks = assets / "Forza Motorsport 4 Full DLC Collection" / "tracks"
    tracks.mkdir()
    (tracks / "pack.bin").write_bytes(_live({"Media/DLCZips/0015_pri_65.zip": _zip({"media/x": b"x"})}, "Tracks"))
    (assets / "other title").write_bytes(_live({"Media/a": b"a"}, "FH1 thing", title=FH1))
    (assets / "save").write_bytes(_live({"Media/a": b"a"}, "A save", content_type=1))
    (assets / "readme.txt").write_text("not a package")
    (assets / "LIVE.iso").write_bytes(b"LIVE" + bytes(100))  # disc images are identify()'s
    return assets


def test_packages_are_found_by_their_header_whatever_they_are_called():
    with tempfile.TemporaryDirectory() as tmp:
        assets = _assets(Path(tmp))
        found = {p.path.name: p for p in forza_roster.dlc_packages(assets)}
        assert set(found) == {"E3470A83732B9093D220BF7D6506FB30517900064D", "pack.bin", "other title", "save"}
        porsche = found["E3470A83732B9093D220BF7D6506FB30517900064D"]
        assert (porsche.game, porsche.name, porsche.where) == ("FM4", "Test Car Pack", "FM4 DLC / Test Car Pack")
        assert found["other title"].game is None and "title ID 4D5309C9" in found["other title"].error
        assert found["save"].game is None and "content type 0x1" in found["save"].error
        assert forza_roster.identify_package(assets / "readme.txt") is None
        names, notes = forza_roster.package_summary(list(found.values()))
        assert sorted(names) == ["Test Car Pack", "Tracks"]
        assert sorted(n.split(" is ")[0] for n in notes) == ["other title", "save"]
        # a second copy of a pack is named once and noted
        copy = Path(tmp) / "copy"
        copy.write_bytes(_car_pack())
        both = forza_roster.dlc_packages(assets) + forza_roster.dlc_packages(copy)
        names, notes = forza_roster.package_summary(both)
        assert names.count("Test Car Pack") == 1 and any("again" in n for n in notes)
        assert len([p for p in forza_roster.unique_packages(both) if p.name == "Test Car Pack"]) == 1


def test_dlc_cars_join_the_roster_only_when_asked():
    with tempfile.TemporaryDirectory() as tmp:
        assets = _assets(Path(tmp))
        cars, _ = forza_roster.roster([])
        assert cars == {}  # the default roster is the discs' (none here)
        cars, notes = forza_roster.roster([], forza_roster.dlc_packages(assets))
        assert set(cars) == {"por_959_87"}
        car = cars["por_959_87"]
        (source,) = car.sources
        assert car.kind == "car" and car.games == "FM4" and car.year == 1987  # from the pack's own Data_Car
        assert source.dlc and source.iso == "" and source.where == "FM4 DLC / Test Car Pack"
        assert source.path == "Media/DLCZips/34_pri_65.zip::media/cars/POR_959_87/"
        assert any("Test Car Pack, 1 cars" in n for n in notes)


def test_names_come_from_the_pack_then_the_disc():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assets = _assets(root)
        porsche = next(p for p in forza_roster.dlc_packages(assets) if p.name == "Test Car Pack")
        info = forza_roster.pack_info(porsche)
        assert info["por_959_87"] == {
            "display": "Porsche 959",
            "model_short": "959",
            "year": 1987,
            "make": "Porsche",
            "family": None,  # List_FamilyModel row 7 isn't in the pack
        }
        # the family's row and text from the disc's gamedb and string tables
        family_strings, family_refs = _lsb2("List_FamilyModel", {"IDS_DisplayName_7": "911, 959"})
        disc_db = root / "gamedb.slt"
        db = sqlite3.connect(disc_db)
        db.execute("CREATE TABLE Data_Car (Id, Year, MakeID, DisplayName, ModelShort, MediaName, FamilyModelID)")
        db.execute("CREATE TABLE List_CarMake (ID, DisplayName)")
        db.execute("CREATE TABLE List_FamilyModel (Id, DisplayName)")
        db.execute("INSERT INTO List_FamilyModel VALUES (7, ?)", (family_refs["IDS_DisplayName_7"],))
        db.commit()
        db.close()
        disc_strings = forza_strings.Strings.of({"List_FamilyModel.str": family_strings})
        info = forza_roster.pack_info(porsche, disc_db, disc_strings)
        assert info["por_959_87"]["family"] == "911, 959" and info["por_959_87"]["make"] == "Porsche"


def test_a_dlc_car_is_extracted_like_a_disc_car():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assets = _assets(root)
        cars, _ = forza_roster.roster([], forza_roster.dlc_packages(assets))
        source = cars["por_959_87"].sources[0]
        cache = root / "cache"
        folder = port_car.extract_car(source, "POR_959_87", cache, log=lambda _: None)
        assert folder == cache / "fm4" / "POR_959_87"
        assert (folder / "por_959_87.carbin").read_bytes() == b"main carbin"
        assert (folder / "physics" / "maxdata.xml").read_bytes() == b"<MAXData/>"
        assert port_car.extract_wheel(source, "POR_959_87", cache, log=lambda _: None) == ["wheel.xds"]
        assert (folder / "wheel.xds").read_bytes() == b"wheel texture"
        entry = port_car.car_entry("POR_959_87", 1987, "FM4", source, cache, 700, 1, "host", {})
        # the manifest names the package by its file name, not this machine's path
        assert entry["source"]["package"] == "E3470A83732B9093D220BF7D6506FB30517900064D"


def test_preflight_lists_the_packs_and_says_they_are_not_built():
    with tempfile.TemporaryDirectory() as tmp:
        assets = _assets(Path(tmp))
        lines = install.dlc_preflight(forza_roster.dlc_packages(assets))
        assert lines[0].startswith("FM4 DLC packs: 2 (")
        assert "Test Car Pack" in lines[0] and "Tracks" in lines[0] and "#170" in lines[0]
        assert any("other title" in line and "skipped" in line for line in lines[1:])
        assert install.dlc_preflight([]) == []


def test_a_car_in_fh1_and_a_dlc_pack_is_built_from_the_version_with_its_files():
    from tests.test_discs import _disc

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        assets = _assets(root)
        files = {name.format(name="por_959_87"): b"x" for name in port_car.WANTED}
        full = {"media/cars/POR_959_87.zip": _zip({"POR_959_87.carbin": b"c", **files})}
        fh1 = _disc(root / "fh1.iso", FH1, files=full)
        cars, _ = forza_roster.roster([fh1], forza_roster.dlc_packages(assets))
        car = cars["por_959_87"]
        assert car.games == "FM4,FH1" and [s.dlc for s in car.sources] == [False, True]
        # the pack's version lacks most converter inputs: FH1's is built, also when asked for none
        _, source = port_car.pick_source(cars, port_car.CarRequest("POR_959_87", None))
        assert source.game == "FH1"
        _, source = port_car.pick_source(cars, port_car.CarRequest("POR_959_87", "FM4"))
        assert source.dlc  # @fm4: the only FM4 version


def test_choose_source_takes_the_complete_version_then_the_game_order():
    fh1 = forza_roster.Source("FH1", "FH1", "media/cars/x.zip", 0, 0, "car", "fh1.iso")
    dlc = forza_roster.Source("FM4", "FM4 DLC / Pack", "a.zip::media/cars/x/", 0, 0, "car", "", "pkg")
    disc = forza_roster.Source("FM4", "FM4 disc 1", "media/cars/x.zip", 0, 0, "car", "fm4.iso")
    wanted = ["x.carbin", "x_cockpit.carbin"]
    listing = {id(fh1): {"x.carbin"}, id(dlc): {"x.carbin", "x_cockpit.carbin"}, id(disc): set(wanted)}
    best, notes = forza_roster.choose_source([fh1, dlc], wanted, ("FH1", "FM4"), lambda s: listing[id(s)])
    assert best is dlc and notes == ["FH1: no x_cockpit.carbin", "FM4 DLC / Pack: all 2 inputs"]
    listing[id(fh1)] = set(wanted)  # a tie: the game order, a disc before a pack
    assert forza_roster.choose_source([dlc, fh1], wanted, ("FH1", "FM4"), lambda s: listing[id(s)])[0] is fh1
    assert forza_roster.choose_source([dlc, disc], wanted, ("FH1", "FM4"), lambda s: listing[id(s)])[0] is disc
