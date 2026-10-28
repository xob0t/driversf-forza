"""Skipping the cars the stock game already has (port_batch --skip-stock-models, dsff
install's default) and the ID map (--id-map) that keeps every car's spool ID the same
whatever is selected: saves key vehicle unlock and owned flags by spool ID.

    uv run python tests/test_stock_models.py     # or every test: uv run python -m tests
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from unittest import mock

from dsfforza.build import install, port_batch, report
from dsfforza.build.port_batch import Pick

# (media, stock counterpart id): makers AAA..DDD, 30 CCC cars (the garage policy's group cap
# overflows), some with a stock model.
CARS = [
    ("AAA_One_10", 1),
    ("AAA_Two_10", None),
    ("BBB_Three_10", 2),
    *[(f"CCC_Car{i}_10", 3 if i % 4 == 0 else None) for i in range(30)],
    ("DDD_Last_10", None),
]
TAKEN = {5, 6, 40, 304}  # the base archive's IDs


def picks(names=None) -> list[Pick]:
    names = names or [n for n, _ in CARS]
    by_name = dict(CARS)
    return [Pick(n, "FM4", 2010, "FM4", {}, counterpart=by_name.get(n), counterpart_model="Stock") for n in names]


def assign(items: list[Pick], policy: str, id_map=None) -> dict[str, int]:
    """{media: ID} from assign_ids with a small stock garage (AAA has 17 of its 19 places)."""
    groups = {p.media: "ID:" + p.media[:3] for p in items}
    with (
        mock.patch.object(port_batch, "garage_groups", return_value=({"ID:AAA": 17}, {})),
        mock.patch.object(port_batch, "car_groups", return_value=groups),
    ):
        port_batch.assign_ids(items, set(TAKEN), policy, 304, None, id_map)
    return {p.media: p.spool_id for p in items}


def test_the_map_gives_a_car_the_same_id_whatever_is_selected():
    names = [n for n, _ in CARS]
    for policy in ("asi", "garage", "high"):
        full = assign(picks(), policy)  # no map: the list order, today's assignment
        assert len(set(full.values())) == len(full) and not set(full.values()) & TAKEN
        selections = {
            "subset": names[3:20],
            "reordered": names[::-1],
            "shuffled subset": [names[i] for i in (30, 4, 1, 17, 9)],
            "skip": [n for n, stock in CARS if stock is None],
        }
        for label, chosen in selections.items():
            got = assign(picks(chosen), policy, dict(full))
            assert got == {n: full[n] for n in chosen}, (policy, label)


def test_without_a_map_the_ids_are_the_list_order_as_before():
    first = assign(picks(), "asi")
    again = assign(picks(), "asi", {})
    assert first == again
    assert first["AAA_One_10"] == 1 and first["AAA_Two_10"] == 2 and 5 not in first.values()
    assert assign(picks(), "high")["AAA_One_10"] == 305  # 304 is the base archive's
    # selection-dependent without a map: the reason for it
    only = assign(picks(["CCC_Car3_10", "DDD_Last_10"]), "asi")
    assert only["CCC_Car3_10"] != first["CCC_Car3_10"]


def test_a_new_car_gets_a_fresh_id_and_a_car_left_out_keeps_its_id_reserved():
    full = assign(picks(), "asi")
    skipped = "CCC_Car4_10"
    chosen = [n for n, _ in CARS if n != skipped] + ["NEW_Car_11"]
    got = assign(picks(chosen), "asi", dict(full))
    assert got["NEW_Car_11"] not in set(full.values()) | TAKEN  # not the skipped car's, nor a taken one
    # and the skipped car comes back with its old ID
    back = assign(picks([skipped, "NEW_Car_11"]), "asi", {**full, "NEW_Car_11": got["NEW_Car_11"]})
    assert back == {skipped: full[skipped], "NEW_Car_11": got["NEW_Car_11"]}


def test_an_id_the_base_archive_uses_or_that_is_used_twice_is_not_kept():
    items = picks(["AAA_One_10", "AAA_Two_10", "BBB_Three_10"])
    got = assign(items, "asi", {"AAA_One_10": 40, "AAA_Two_10": 700, "BBB_Three_10": 700})
    assert got["AAA_One_10"] != 40 and got["AAA_Two_10"] == 700 and got["BBB_Three_10"] not in (700, 40)
    assert len(set(got.values())) == 3


def test_the_garage_policy_counts_the_kept_cars_in_their_groups():
    full = assign(picks(), "garage")
    low = {n for n, i in full.items() if i < 62}
    assert 0 < len(low) <= 61
    # only the AAA/BBB/first CCC cars have room (17 + 19 per group): the rest are high IDs
    got = assign(picks(["DDD_Last_10", "CCC_Car29_10", "AAA_One_10"]), "garage", dict(full))
    assert got == {n: full[n] for n in got}


def test_the_map_file_keeps_the_rows_of_cars_not_built():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ids.json"
        assert port_batch.read_id_map(path) == {}
        old = {"OUT_1": 7, "aaa_one_10": 1}
        items = picks(["AAA_One_10"])
        items[0].spool_id = 3
        new = port_batch.write_id_map(path, old, items)
        assert new == {"OUT_1": 7, "AAA_One_10": 3}  # a skipped car's row stays; a changed one is replaced
        assert port_batch.read_id_map(path) == new


def args(**kw) -> argparse.Namespace:
    base = {
        "car": None,
        "list": None,
        "game": None,
        "exclude": None,
        "make": None,
        "body": None,
        "year": None,
        "no_counterpart": False,
        "counterpart": False,
        "diverse": None,
        "max": None,
        "skip_stock_models": True,
    }
    return argparse.Namespace(**{**base, **kw})


def select(**kw):
    a = args(**kw)
    with (
        mock.patch.object(port_batch, "describe_cars", side_effect=lambda cars, requests, dsf, dbs: picks([m for m, _ in requests])),
        mock.patch.object(port_batch, "read_list", return_value=[(n, None) for n, _ in CARS]),
    ):  # fmt: skip
        return port_batch.select(a, {}, Path("x"), {}), a


def test_select_skips_stock_models_from_a_list_but_keeps_a_car_named_with_car():
    out, a = select(list=[Path("all.txt")])
    stock = [n for n, cp in CARS if cp]
    assert {p.media for p in out} == {n for n, _ in CARS} - set(stock)
    assert [c["media"] for c in a.stock_models_skipped] == stock
    assert a.stock_models_skipped[0] == {"media": "AAA_One_10", "stock_id": 1, "stock_model": "Stock"}
    out, a = select(list=[Path("all.txt")], car=["aaa_one_10@fm4"])
    assert "AAA_One_10" in {p.media for p in out}  # an explicit --car wins (the list has it too)
    assert "AAA_One_10" not in [c["media"] for c in a.stock_models_skipped]
    out, a = select(list=[Path("all.txt")], skip_stock_models=False)
    assert len(out) == len(CARS) and a.stock_models_skipped == []


def test_the_parsers_take_the_options():
    build = port_batch.build_parser().parse_args(["build", "--out", "x", "--skip-stock-models", "--id-map", "m.json"])
    assert build.skip_stock_models is True and build.id_map == Path("m.json")
    plain = port_batch.build_parser().parse_args(["plan"])
    assert plain.skip_stock_models is False and plain.id_map is None
    assert install.build_parser().parse_args([]).with_stock_models is False
    assert install.build_parser().parse_args(["--with-stock-models"]).with_stock_models is True


def test_the_summary_and_the_report_name_the_left_out_cars():
    with tempfile.TemporaryDirectory() as tmp:
        build = Path(tmp)
        rows = [
            {"media": "NIS_370Z_10", "stock_id": 227, "stock_model": "Nissan_370Z_2009_Civ"},
            {"media": "FOR_GT_05", "stock_id": 189, "stock_model": "Ford_GT_2006"},
        ]
        data = {"ok": [{"media": "A"}], "failed": [], "stock_models_skipped": rows}
        (build / "batch-report.json").write_text(json.dumps(data), encoding="utf-8")
        (build / "dngvehicles.json").write_text(json.dumps({"batch": {"skip_stock_models": True}}), encoding="utf-8")
        s = install.Summary(build_dir=build, in_archive=1, built=1, stock_models=["NIS_370Z_10", "FOR_GT_05"])
        text = "\n".join(install.summary_lines(s))
        assert "Left out:  2 cars the stock game already has" in text and "--with-stock-models" in text
        info = report.build_info(build, {})
        assert [c["media"] for c in info["stock_models_skipped"]] == ["NIS_370Z_10", "FOR_GT_05"]
        head = {"report": {"created": "2026-01-01T00:00:00"}, "version": {"kind": "?"}, "build": info}
        lines = report.summary_lines(head)
        assert any(line.startswith("Left out:") and "NIS_370Z_10, FOR_GT_05" in line for line in lines)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
