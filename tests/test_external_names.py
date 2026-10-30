"""Names that come from outside the repo keep their spelling (issue #191).

PR #169 renamed "stock" to "vanilla" everywhere and also rewrote Forza's own
gamedb column `IsStock`, so every car's handling fell back to its host's."""

from dsfforza import REPO

# Forza's own names: gamedb columns and CarAttribs fields.
FORZA_NAMES = {
    "src/dsfforza/build/handling.py": ["IsStock = 1"],
    "src/dsfforza/convert/forza2dsf/car.py": ["FrontStockRideHeight", "RearStockRideHeight"],
}


def test_forza_names_are_not_renamed():
    for path, names in FORZA_NAMES.items():
        text = (REPO / path).read_text(encoding="utf-8")
        for name in names:
            assert name in text, f"{path} lost Forza's name {name!r}"
    handling = (REPO / "src/dsfforza/build/handling.py").read_text(encoding="utf-8")
    assert "IsVanilla" not in handling, "gamedb has no IsVanilla column"
