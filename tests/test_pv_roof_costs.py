"""Unit tests for model/pv_roof_costs.py: cost-class pricing of per-plant rooftop PV.

The module turns the class of each candidate (from units.csv) and the class
cost table into an annuitised investment slope per plant. Every failure mode
is an error, never a default, so each test here checks either the number a
plant gets or the error it raises.
"""
import pandas as pd
import pytest

import input.cost_operation_invest_data as cost_params
import model.pv_roof_costs as costs
from utils.annuity import DISCOUNT_RATE, annuitise

TABLE_COLUMNS = ["cost_class", "year", "investment_cost_chfMW", "source"]


def _table(rows):
    return pd.DataFrame(rows, columns=TABLE_COLUMNS)


# --- validate_cost_mode -------------------------------------------------------

def test_validate_cost_mode_accepts_the_three_modes_with_the_flag_on():
    for mode in costs.COST_MODES:
        assert costs.validate_cost_mode(mode, True) == mode
    assert costs.validate_cost_mode("tech", False) == "tech"


def test_validate_cost_mode_rejects_unknown_modes_and_class_modes_without_the_flag():
    with pytest.raises(ValueError, match="not one of"):
        costs.validate_cost_mode("by_size", True)
    for mode in ("by_class", "average"):
        with pytest.raises(ValueError, match="needs high_resolution_PV"):
            costs.validate_cost_mode(mode, False)


# --- read_cost_table ----------------------------------------------------------

def test_read_cost_table_rejects_missing_file_missing_column_and_repeated_pairs(tmp_path):
    with pytest.raises(FileNotFoundError, match="not found"):
        costs.read_cost_table(str(tmp_path / "missing.csv"))

    no_cost = tmp_path / "no_cost.csv"
    _table([("all", 2050, 1.0, "s")]).drop(columns="investment_cost_chfMW").to_csv(no_cost, index=False)
    with pytest.raises(ValueError, match="lacks column"):
        costs.read_cost_table(str(no_cost))

    repeated = tmp_path / "repeated.csv"
    _table([("all", 2050, 1.0, "s"), ("all", 2050, 2.0, "s")]).to_csv(repeated, index=False)
    with pytest.raises(ValueError, match=r"repeats \(cost_class, year\)"):
        costs.read_cost_table(str(repeated))


def test_read_cost_table_returns_integer_years(tmp_path):
    path = tmp_path / "t.csv"
    _table([("all", 2050, 1.0, "s"), ("1-10kW", 2035, 2.0, "s")]).to_csv(path, index=False)
    table = costs.read_cost_table(str(path))
    assert table["year"].tolist() == [2050, 2035]
    assert str(table["year"].dtype).startswith("int")


# --- class_investment_slopes --------------------------------------------------

def test_class_investment_slopes_annuitise_every_class_of_the_run_year():
    table = _table([("a", 2050, 1_000_000, ""), ("b", 2050, 500_000, ""), ("a", 2035, 2_000_000, "")])

    slopes = costs.class_investment_slopes(table, 2050, amortization_years=25, discount_rate=0.05)

    assert set(slopes) == {"a", "b"}
    assert slopes["a"] == pytest.approx(annuitise(1_000_000, 25, 0.05))
    assert slopes["a"] == pytest.approx(70_952.46, rel=1e-6)     # 1 MCHF over 25 years at 5 %
    assert slopes["b"] == pytest.approx(slopes["a"] / 2)
    # the amortization period is part of the price, not a cosmetic argument
    assert costs.class_investment_slopes(table, 2050, amortization_years=30, discount_rate=0.05)["a"] < slopes["a"]


def test_class_investment_slopes_default_to_the_pvrf_amortization_period():
    table = _table([("a", 2050, 1_000_000, "")])
    pvrf_years = cost_params.amortization_years_all["pvrf"]
    assert pvrf_years > 0
    assert costs.class_investment_slopes(table, 2050)["a"] == pytest.approx(
        annuitise(1_000_000, pvrf_years, DISCOUNT_RATE)
    )


def test_class_investment_slopes_rejects_a_run_year_without_rows():
    table = _table([("a", 2050, 1_000_000, "")])
    with pytest.raises(ValueError, match=r"no cost-class rows for run_year=2040.*\[2050\]"):
        costs.class_investment_slopes(table, 2040)


# --- plant_cost_classes -------------------------------------------------------

def test_plant_cost_classes_follow_the_mode():
    unit_classes = {"CH00_a_pvrf": "1-10kW", "CH00_b_pvrf": "100-1000kW"}
    assert costs.plant_cost_classes("tech", unit_classes) == {}
    assert costs.plant_cost_classes("tech", None) == {}
    assert costs.plant_cost_classes("by_class", unit_classes) == unit_classes
    assert costs.plant_cost_classes("average", unit_classes) == {"CH00_a_pvrf": "all", "CH00_b_pvrf": "all"}


def test_plant_cost_classes_need_the_column_outside_mode_tech():
    with pytest.raises(ValueError, match="no `cost_class` column"):
        costs.plant_cost_classes("by_class", None)
    with pytest.raises(ValueError, match="no `cost_class` column"):
        costs.plant_cost_classes("average", None)


# --- plant_investment_slopes --------------------------------------------------

def test_plant_investment_slopes_price_each_plant_by_its_class():
    table = _table([("1-10kW", 2050, 1_000_000, ""), ("all", 2050, 400_000, "")])
    by_class = costs.class_investment_slopes(table, 2050)

    slopes = costs.plant_investment_slopes({"p1": "1-10kW", "p2": "all", "p3": "1-10kW"}, 2050, table=table)

    assert slopes == {"p1": by_class["1-10kW"], "p2": by_class["all"], "p3": by_class["1-10kW"]}
    assert costs.plant_investment_slopes({}, 2050, table=table) == {}


def test_plant_investment_slopes_reject_a_class_the_table_lacks():
    table = _table([("all", 2050, 400_000, "")])
    with pytest.raises(ValueError, match=r"\['10-30kW'\] of the units have no row for run_year=2050"):
        costs.plant_investment_slopes({"p1": "10-30kW", "p2": "all"}, 2050, table=table)


# --- the shipped table --------------------------------------------------------

def test_shipped_table_covers_both_run_years_and_its_average_row_is_the_weighted_mean():
    table = costs.read_cost_table()
    classes = {"1-10kW", "10-30kW", "30-100kW", "100-1000kW", "all"}
    # installable potential per class of pv09_ewopt/2009 (MW), as stated in the table's source column
    weights = {"1-10kW": 26268.5, "10-30kW": 36125.4, "30-100kW": 20053.1, "100-1000kW": 10856.5}
    for year in (2035, 2050):
        rows = table[table["year"] == year].set_index("cost_class")["investment_cost_chfMW"]
        assert set(rows.index) == classes
        assert rows["1-10kW"] > rows["10-30kW"] > rows["30-100kW"] > rows["100-1000kW"]
        mean = sum(weights[c] * rows[c] for c in weights) / sum(weights.values())
        assert rows["all"] == pytest.approx(mean, abs=500)        # rounded to 1,000 CHF/MW
