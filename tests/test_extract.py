"""port_car extraction rules on file-name lists only (no discs needed).

uv run python tests/test_extract.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

from dsfforza.build import port_car


def test_colour_variant_when_nodamage_missing():
    # CHE_CamaroZ28_79 (FM4 1950-2000 pack): only colour variants of nodamage
    names = {
        "che_camaroz28_79.carbin",
        "damage.xds",
        "lights.xds",
        "nodamage_yellow.xds",
        "nodamage_yellow_lod0.xds",
        "nodamage_blue.xds",
        "nodamage_blue_lod0.xds",
        "nodamage_gold.xds",
        "nodamage_gold_lod0.xds",
    }
    written = {"che_camaroz28_79.carbin", "lights.xds"}
    assert port_car.colour_variants(names, written) == {
        "nodamage.xds": "nodamage_blue.xds",
        "nodamage_lod0.xds": "nodamage_blue_lod0.xds",
    }


def test_colour_variant_without_separator():
    # PON_TransAm_73: nodamagegreen, nodamagegreenb, nodamagered...
    names = {"nodamagegreen.xds", "nodamagegreen_lod0.xds", "nodamagegreenb.xds", "nodamagered.xds"}
    assert port_car.colour_variants(names, set()) == {
        "nodamage.xds": "nodamagegreen.xds",
        "nodamage_lod0.xds": "nodamagegreen_lod0.xds",
    }


def test_no_variant_when_nodamage_present():
    # 571 of 573 cars: nodamage.xds is there, variants (stripes, colours) are ignored
    names = {"nodamage.xds", "nodamage_lod0.xds", "nodamage_nostripe.xds", "nodamageblack.xds"}
    assert port_car.colour_variants(names, {"nodamage.xds", "nodamage_lod0.xds"}) == {}
    assert port_car.colour_variants({"lights.xds"}, set()) == {}


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
