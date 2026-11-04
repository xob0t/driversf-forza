"""A FM4 DLC car's engine sound from its own pack (issue #457): the pack's loose banks and the
_ET.xml / _HT.xml in its inner zip, read before the discs; the HT files and banks the pack
doesn't carry come from the discs. Tiny synthetic discs and packages; no game data is read."""

from __future__ import annotations

import struct
import tempfile
import wave
from pathlib import Path
from unittest import mock

from tests.test_discs import FM4, _disc
from tests.test_dlc import _live, _zip

from dsfforza.convert import engine_sound
from dsfforza.formats import forza_audio

ENGINES = "Media/audio/cars/Engines"
LOD1 = f"{ENGINES}/Soundbanks/LOD1"


def _fsb(name: str) -> bytes:
    """An FSB4 bank with one 16-bit mono subsong called `name` (parse_fsb reads its header)."""
    data = bytes(64)
    sub = bytearray(0x50)
    struct.pack_into("<H", sub, 0, 0x50)
    sub[2 : 2 + len(name)] = name.encode()
    struct.pack_into("<IIIIIi", sub, 32, 32, len(data), 0, 31, 0x10 | 0x20, 32000)
    struct.pack_into("<H", sub, 62, 1)
    head = b"FSB4" + struct.pack("<IIIII", 1, len(sub), len(data), 0x40000, 0) + bytes(24)
    return head + bytes(sub) + data


def _et(ht: dict[str, str]) -> bytes:
    nodes = "".join(f'<{tag}><Upgrade L0="{name}"/></{tag}>' for tag, name in ht.items())
    return (
        f'<EngineTuning><EngineSettings audio_rpm_idle="900" audio_rpm_redline="7000"/>{nodes}</EngineTuning>'
    ).encode()


def _ht(bank: str, group: str, rpm: int) -> bytes:
    return (
        f'﻿<Soundbank name="{bank}"><{group}><Loop wavebankindex="0" rpm_min="{rpm - 100}" '
        f'rpm_sample="{rpm}" rpm_max="{rpm + 100}" volume="0.8"/></{group}></Soundbank>'
    ).encode()


def _setup(root: Path) -> tuple[Path, Path]:
    """FM4 disc 1 with a disc car (DISC_Car_00, whose HT the pack also ships, changed) and a
    shared intake HT and bank; a DLC pack with POR_959_87, whose ET is loose in the inner zip,
    its exhaust bank loose in the package, and its intake HT and bank only on the disc."""
    disc = _disc(
        root / "fm4 disc 1.iso",
        FM4,
        1,
        2,
        files={
            f"{ENGINES}/enginetuning.zip": _zip(
                {"DISC_Car_00_ET.xml": _et({"Exhaust": "Shared_Exh_HT.xml", "EngineIntake": "Shared_Int_HT.xml"})}
            ),
            f"{ENGINES}/harmonictuning.zip": _zip(
                {
                    "Shared_Exh_HT.xml": _ht("Shared_Exh.fsb", "ExhaustL", 3000),
                    "Shared_Int_HT.xml": _ht("Shared_Int.fsb", "EngineIntake", 4000),
                }
            ),
            f"{ENGINES}/enginetuning/dummy.txt": b"dummy for folder structure\r\n",
            f"{LOD1}/Shared_Exh.fsb": _fsb("disc exhaust"),
            f"{LOD1}/Shared_Int.fsb": _fsb("disc intake"),
        },
    )
    inner = _zip(
        {
            "media/cars/POR_959_87/por_959_87.carbin": b"main carbin",
            "media/audio/cars/Engines/EngineTuning/POR_959_87_ET.xml": _et(
                {"Exhaust": "Shared_Exh_HT.xml", "EngineIntake": "Shared_Int_HT.xml"}
            ),
            "media/audio/cars/Engines/HarmonicTuning/Shared_Exh_HT.xml": _ht("6T0_959_Exh.fsb", "ExhaustL", 3500),
        }
    )
    pack = root / "assets" / "E3470A83732B9093D220BF7D6506FB30517900064D"
    pack.parent.mkdir(parents=True)
    pack.write_bytes(
        _live(
            {
                "Media/DLCZips/34_pri_65.zip": inner,
                "Media/DLCZips/34_pri_65/Media/audio/cars/Engines/Soundbanks/LOD1/6T0_959_Exh.fsb": _fsb(
                    "pack exhaust"
                ),
            },
            "Porsche Expansion Pack",
        )
    )
    return disc, pack


def _fake_decode(_fsb_path: Path, fsb, folder: Path, _vgm: Path) -> list[Path]:
    """Stands in for vgmstream: one silent WAV per subsong, named as decode_bank names them."""
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for s in fsb.subsongs:
        path = folder / f"{s.index:02d}_{s.name.replace(' ', '_')}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(s.rate)
            w.writeframes(bytes(2 * s.samples))
        out.append(path)
    return out


def _engine(car: str, disc: Path, cache: Path, package: Path | None = None) -> dict:
    with (
        mock.patch.object(engine_sound, "_decoded", _fake_decode),
        mock.patch.object(forza_audio, "vgmstream_path", lambda _given: Path("vgmstream-cli.exe")),
    ):
        return engine_sound.forza_engine(car, "fm4", [disc], cache, package=package)


def test_a_package_file_lists_its_loose_banks_and_the_tuning_in_its_inner_zip():
    with tempfile.TemporaryDirectory() as tmp:
        _, pack = _setup(Path(tmp))
        source = forza_audio.Source(str(pack))
        try:
            names = sorted(f.name for f in source.files)
            assert names == ["6T0_959_Exh.fsb", "POR_959_87_ET.xml", "Shared_Exh_HT.xml"], names
            assert [f.name for f in forza_audio.engine_banks(source.files)] == ["6T0_959_Exh.fsb"]
            tuning = forza_audio.Tuning([source])
            assert tuning.engine("POR_959_87").settings["audio_rpm_redline"] == "7000"
            ht, _ = tuning.harmonic("Shared_Exh_HT.xml")
            assert ht.bank == "6T0_959_Exh.fsb"
            assert ht.file == "media/audio/cars/Engines/HarmonicTuning/Shared_Exh_HT.xml"
        finally:
            source.close()


def test_a_dlc_car_takes_its_packs_files_first_and_the_rest_from_the_disc():
    with tempfile.TemporaryDirectory() as tmp:
        disc, pack = _setup(Path(tmp))
        cache = Path(tmp) / "cache"
        forza = _engine("POR_959_87", disc, cache, pack)
        assert forza["et"] == "media/audio/cars/Engines/EngineTuning/POR_959_87_ET.xml"
        assert (forza["idle"], forza["redline"]) == (900.0, 7000.0)
        exhaust, intake = forza["emissions"]["Exhaust"], forza["emissions"]["EngineIntake"]
        # the pack's Shared_Exh_HT.xml wins over the disc's, and names the pack's bank
        assert (exhaust["bank"], [lp.rpm for lp in exhaust["loops"]]) == ("6T0_959_Exh.fsb", [3500])
        # the pack has no intake HT or bank: the disc's
        assert (intake["bank"], [lp.rpm for lp in intake["loops"]]) == ("Shared_Int.fsb", [4000])
        folder = cache / "fm4" / "dlc" / engine_sound.package_content_id(pack)
        assert (folder / "6T0_959_Exh.fsb").read_bytes() == _fsb("pack exhaust")
        assert (cache / "fm4" / "Shared_Int.fsb").read_bytes() == _fsb("disc intake")


def test_a_disc_car_never_reads_the_pack():
    with tempfile.TemporaryDirectory() as tmp:
        disc, _pack = _setup(Path(tmp))
        cache = Path(tmp) / "cache"
        forza = _engine("DISC_Car_00", disc, cache)
        assert forza["emissions"]["Exhaust"]["bank"] == "Shared_Exh.fsb"
        assert [lp.rpm for lp in forza["emissions"]["Exhaust"]["loops"]] == [3000]
        assert not (cache / "fm4" / "dlc").exists()
        # without its pack, the DLC car has no tuning
        try:
            _engine("POR_959_87", disc, cache)
        except LookupError as error:
            assert "POR_959_87_ET.xml" in str(error)
        else:
            raise AssertionError("a DLC car's ET was found without its pack")


def test_a_bank_missing_everywhere_says_where_it_was_looked_for():
    with tempfile.TemporaryDirectory() as tmp:
        disc, pack = _setup(Path(tmp))
        with mock.patch.object(forza_audio.Source, "find_name", lambda _self, _name: []):
            forza = _engine("POR_959_87", disc, Path(tmp) / "cache", pack)
            disc_car = _engine("DISC_Car_00", disc, Path(tmp) / "cache")
        assert forza["missing"]["Exhaust"] == "bank 6T0_959_Exh.fsb not in its pack or on the FM4 discs"
        assert disc_car["missing"]["Exhaust"] == "bank Shared_Exh.fsb not on the FM4 discs"


def test_pack_banks_are_cached_by_the_packs_content_id_not_its_file_name():
    with tempfile.TemporaryDirectory() as tmp:
        ids = []
        for n, folder in enumerate(("a", "b")):
            pack = Path(tmp) / folder / "pack"  # the same file name in two folders
            pack.parent.mkdir()
            head = bytearray(0x400)
            head[:4] = b"LIVE"
            head[0x32C : 0x32C + 20] = bytes([n + 1]) * 20
            pack.write_bytes(bytes(head))
            ids.append(engine_sound.package_content_id(pack))
        assert ids == ["01" * 20, "02" * 20], ids
