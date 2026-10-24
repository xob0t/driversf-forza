"""engine_sound.nearest_tuning: which EngineTuning file a car without its own takes.

uv run python tests/test_engine_tuning.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.convert import engine_sound as es

STEMS = [
    "vw_corrado_95",
    "vw_gtivr6_98",
    "hyu_genesiscoupe_09",
    "hyu_genesiscoupermr_10",
    "sal_s281_00",
    "sal_s281e_06",
    "aud_q7v12tdi_09",
    "aud_rs6_09",
    "maz_16_b09_10",
    "maz_x_b09_09",
    "che_corvette_62",
    "che_corvettegs_10",
]


def test_media_parts():
    assert es._media_parts("CHE_Corvette_60") == ("che", "corvette", 60)
    assert es._media_parts("MAZ_16_B09_09") == ("maz", "16_b09", 9)
    assert es._media_parts("BUS_TOUR_12") == ("bus", "tour", 12)


def test_same_model_nearest_year():
    assert es.nearest_tuning("HYU_GenesisCoupe_10", STEMS) == "hyu_genesiscoupe_09"
    assert es.nearest_tuning("MAZ_16_B09_09", STEMS) == "maz_16_b09_10"
    assert es.nearest_tuning("sal_s281_06", STEMS) == "sal_s281_00"  # same model before a longer name
    assert es.nearest_tuning("CHE_Corvette_60", STEMS) == "che_corvette_62"


def test_contained_model():
    assert es.nearest_tuning("VW_IECorrado_95", STEMS) == "vw_corrado_95"
    assert es.nearest_tuning("AUD_Q7_09", STEMS) is None  # "q7" is too short to match by containment


def test_no_match():
    assert es.nearest_tuning("FER_Enzo_02", STEMS) is None
    assert es.nearest_tuning("VW_Corrado_95", ["vw_corrado_95"]) is None  # never itself


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
