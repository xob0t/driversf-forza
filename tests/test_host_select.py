"""Checks for src/dsfforza/build/host_select.py that need no game files.

uv run python tests/test_host_select.py     # or: uv run python -m tests host_select
"""

from __future__ import annotations

from dsfforza.build import host_select as hs


def _host(model, picture, year, body, wheelbase, length):
    return {
        "model": model,
        "body": body,
        "wheelbase": wheelbase,
        "length": length,
        "stats": {"Picture1": picture, "Year": year},
    }


def _car(media, make, model, year, body, wheelbase, length):
    return hs.ForzaInfo(
        media,
        "FH1",
        year,
        make,
        model,
        hs.Engine(8, "V", "na", 200.0),
        "RWD",
        body,
        4,
        2000.0,
        wheelbase,
        1.8,
        length,
        1.8,
    )


# Vanilla hosts and Forza cars as the survey and gamedb.slt give them.
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


def test_door_count_from_carbin_parts():
    # Measured on the cars (LOD 1, z from the wheelbase midpoint backwards): left panes,
    # body panels behind the front door glass, door handle centres; issue #208.
    wrx_04 = (
        [(-0.54, 0.31), (0.29, 1.02)],
        [(0.21, 0.92, 0.09, 0.37), (0.21, 1.16, 0.37, 0.69)],
        [(0.03, 0.66), (0.96, 0.7)],
    )
    # The rear-door pane starts where the front one ends; the first rule merged them (2 doors).
    assert hs.door_count(*wrx_04, 2.54, "sedan") == 4
    rover = ([(-0.42, 0.46), (0.52, 1.32), (1.31, 2.13)], [(0.37, 1.36, 0.45, 0.84)], [(0.2, 0.84), (1.17, 0.86)])
    assert hs.door_count(*rover, 2.90, "suv") == 4  # front glass ends 0.46 m back, on a 2.90 m wheelbase
    clio = ([(-0.63, 0.34), (0.43, 1.16)], [(0.31, 1.58, 0.13, 1.27)], [(0.24, 0.64)])
    assert hs.door_count(*clio, 2.59, "hatch") == 2  # rear quarter past the rear axle
    gti_vr6 = ([(-0.59, 0.37), (0.49, 1.29)], [(0.40, 1.23, 0.42, 0.82)], [(0.26, 0.65)])
    assert hs.door_count(*gti_vr6, 2.47, "hatch") is None  # quarter split at the arch, no handle on it
    p308 = ([(-0.86, 0.22), (0.32, 1.10)], [(0.14, 1.20, 0.12, 1.26)], [(-0.02, 0.67), (0.96, 0.75)])
    assert hs.door_count(*p308, 2.54, "hatch") == 4
    assert hs.door_count(*p308, 2.54, "coupe") is None  # 2-door bodies never get 4
    fiesta = ([(-0.83, -0.69), (-0.62, 0.49), (0.57, 1.23)], [(0.32, 1.61, 0.13, 1.08)], [(0.26, 0.71)])
    assert hs.door_count(*fiesta, 2.50, "hatch") == 2  # vent pane ahead of the door glass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
