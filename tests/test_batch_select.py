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
