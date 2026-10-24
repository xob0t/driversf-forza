"""Checks for tools/host_select.py that need no game files.

    python tools/tests/test_host_select.py     # or: python -m tools.tests host_select
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import host_select as hs  # noqa: E402


def _host(model, picture, year, body, wheelbase, length):
    return {"model": model, "body": body, "wheelbase": wheelbase, "length": length,
            "stats": {"Picture1": picture, "Year": year}}


def _car(media, make, model, year, body, wheelbase, length):
    return hs.ForzaInfo(media, "FH1", year, make, model, hs.Engine(8, "V", "na", 200.0), "RWD", body, 4,
                        2000.0, wheelbase, 1.8, length, 1.8)


# Stock hosts and Forza cars as the survey and gamedb.slt give them.
SAVANA = _host("GMC_Savana_2005", "gmc_savana_news_van", 2005, "van", 3.4323, 5.706)
CHARGER = _host("Dodge_Charger_SRT_8_2009_civ", "dodge_charger_srt8", 2009, "sedan", 3.0481, 5.087)
F350 = _host("Ford_F350_SuperDuty_2008", "ford_f350_super_duty", 2008, "pickup", 4.0501, 6.284)
Q7 = _host("Audi_Q7_2010", "audi_q7", 2010, "suv", 2.9911, 5.076)
EXPRESS = _car("CHE_Express2500Cargo_05", "chevrolet", "express2500cargo", 2005, "van", 3.429, 5.741)


def test_badge_twin_and_same_model():
    assert "badge twin" in hs.same_vehicle(SAVANA, EXPRESS)
    f350 = _car("FOR_F350SuperDuty_08", "ford", "f350superduty", 2008, "pickup", 4.010, 6.326)
    assert "same model" in hs.same_vehicle(F350, f350)


def test_platform_mates_and_other_cars_are_not_the_same_vehicle():
    # 300C on the Charger: one platform and wheelbase, different body length and cabin.
    c300 = _car("CHR_300C_08", "chrysler", "300c", 2008, "sedan", 3.048, 5.162)
    assert hs.same_vehicle(CHARGER, c300) is None
    durango = _car("DOD_Durango_99", "dodge", "durango", 1999, "suv", 2.893, 5.151)
    assert hs.same_vehicle(Q7, durango) is None
    # Same twin, other generation or other body class.
    assert hs.same_vehicle(SAVANA, _car("x", "chevrolet", "express", 1996, "van", 3.429, 5.741)) is None
    assert hs.same_vehicle(SAVANA, _car("x", "chevrolet", "express", 2005, "pickup", 3.429, 5.741)) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
