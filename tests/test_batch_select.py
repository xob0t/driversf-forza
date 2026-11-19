"""port_batch's selection: the requests a command line names, the roster filters, one pick per car."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path
from types import SimpleNamespace

from dsfforza.build import port_batch


def _args(**given) -> argparse.Namespace:
    blank = {
        "car": None,
        "list": None,
        "fm4_dlc_cars": False,
        "game": None,
        "make": None,
        "body": None,
        "year": None,
        "counterpart": False,
        "no_counterpart": False,
    }
    return argparse.Namespace(**{**blank, **given})


CARS = {
    "bmw_m3_08": SimpleNamespace(name="BMW_M3_08", games="FH1,FM4"),
    "aud_r8_08": SimpleNamespace(name="AUD_R8_08", games="FM4"),
    "nis_gtr_12": SimpleNamespace(name="NIS_GTR_12", games="FH1"),
}


def test_named_cars_keep_their_order_and_game():
    with tempfile.TemporaryDirectory() as tmp:
        listed = Path(tmp) / "cars.txt"
        listed.write_text("NIS_GTR_12@fh1  # a comment\n\n# only a comment\nAUD_R8_08\n", encoding="utf-8")
        requests, named = port_batch.selection_requests(_args(car=["BMW_M3_08@fm4"], list=[listed]), CARS)
    assert named
    assert requests == [("BMW_M3_08", "FM4"), ("NIS_GTR_12", "FH1"), ("AUD_R8_08", None)]


def test_without_names_the_roster_by_name_of_the_game():
    requests, named = port_batch.selection_requests(_args(), CARS)
    assert not named
    assert requests == [("AUD_R8_08", None), ("BMW_M3_08", None), ("NIS_GTR_12", None)]
    requests, _ = port_batch.selection_requests(_args(game="fh1"), CARS)
    assert requests == [("BMW_M3_08", "FH1"), ("NIS_GTR_12", "FH1")]


def _pick(media, **given) -> port_batch.Pick:
    fields = {"game": "FM4", "year": 2008, "games": "FM4", "source": {}, "make": "BMW", "body": "coupe", **given}
    return port_batch.Pick(media, **fields)


def test_roster_filter():
    keep = port_batch.roster_filter(_args())
    assert keep(_pick("BMW_M3_08"))
    assert not keep(_pick("BMW_M3_08", notes=["no usable host"]))
    assert not keep(_pick("BMW_M3_08", body="race"))  # race cars only when --body names them
    assert port_batch.roster_filter(_args(body="race,suv"))(_pick("BMW_M3_08", body="race"))
    assert not port_batch.roster_filter(_args(body="suv"))(_pick("BMW_M3_08"))
    assert not port_batch.roster_filter(_args(game="fh1"))(_pick("BMW_M3_08"))
    # --make: the make name or the media name's make code
    assert port_batch.roster_filter(_args(make="bmw"))(_pick("BMW_M3_08", make="Bayerische"))
    assert port_batch.roster_filter(_args(make="Bayerische, NIS"))(_pick("BMW_M3_08", make="Bayerische"))
    assert not port_batch.roster_filter(_args(make="NIS"))(_pick("BMW_M3_08"))
    years = port_batch.roster_filter(_args(year="1960-1979"))
    assert years(_pick("FOR_MUS_69", year=1969))
    assert not years(_pick("BMW_M3_08"))
    assert not years(_pick("FOR_MUS_69", year=None))
    assert port_batch.roster_filter(_args(year="-1999"))(_pick("FOR_MUS_69", year=1969))
    assert not port_batch.roster_filter(_args(counterpart=True))(_pick("BMW_M3_08"))
    assert port_batch.roster_filter(_args(counterpart=True))(_pick("BMW_M3_08", counterpart=80))
    assert not port_batch.roster_filter(_args(no_counterpart=True))(_pick("BMW_M3_08", counterpart=80))
    both = port_batch.roster_filter(_args(counterpart=True, no_counterpart=True))
    assert not both(_pick("BMW_M3_08")) and not both(_pick("BMW_M3_08", counterpart=80))


def test_unique_cars_keeps_the_first_of_each():
    picks = [_pick("BMW_M3_08"), _pick("bmw_m3_08", game="FH1"), _pick("AUD_R8_08")]
    assert [(p.media, p.game) for p in port_batch.unique_cars(picks)] == [("BMW_M3_08", "FM4"), ("AUD_R8_08", "FM4")]


def _ids(picks, policy: str) -> dict[str, int | None]:
    """assign_ids without the vanilla garage tables (no maker groups)."""
    saved = port_batch.garage_groups, port_batch.car_groups
    port_batch.garage_groups = lambda: ({}, {})
    port_batch.car_groups = lambda *_: {}
    try:
        port_batch.assign_ids(picks, {2, 5}, policy, 900)
    finally:
        port_batch.garage_groups, port_batch.car_groups = saved
    return {p.media: p.spool_id for p in picks}


def test_a_car_left_out_keeps_its_id_slot():
    """A listed car that can't be built (its disc not given, its DLC pack not there) uses up
    its ID: when the FM4 discs are added, the FH1 cars after it keep theirs, and with them
    their converted copies (the build ID is part of the reuse rule)."""
    for policy in ("asi", "high"):
        fh1_only = [_pick("A_1"), _pick("B_2", notes=["not on the FM4 discs"]), _pick("C_3"), _pick("D_4")]
        both = [_pick("A_1"), _pick("B_2"), _pick("C_3"), _pick("D_4")]
        before, after = _ids(fh1_only, policy), _ids(both, policy)
        assert before["B_2"] is None
        assert {m: i for m, i in after.items() if m != "B_2"} == {m: i for m, i in before.items() if m != "B_2"}
    assert after == {"A_1": 900, "B_2": 901, "C_3": 902, "D_4": 903}
    assert _ids(both, "asi") == {"A_1": 1, "B_2": 3, "C_3": 4, "D_4": 6}  # 2 and 5 taken


def test_dlc_cars_whose_pack_is_missing_hold_their_place():
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "fm4-dlc.txt"
        listing.write_text("# pack\nPOR_959_87\nMIS_Sing_12\nAUD_R8_12\n", encoding="utf-8")
        found = [_pick("POR_959_87"), _pick("AUD_R8_12")]
        slots = port_batch.dlc_slots(found, listing)
    assert [p.media for p in slots] == ["POR_959_87", "MIS_Sing_12", "AUD_R8_12"]
    assert slots[0] is found[0] and slots[2] is found[1]
    assert slots[1].placeholder and slots[1].notes and slots[1].spool_id is None
