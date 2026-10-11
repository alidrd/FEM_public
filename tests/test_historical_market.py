"""Unit tests for the neighbour prices and transfer limits of a back-cast (read_market in
data_prep/historical_import.py), checked against the production line-limit rule. No model is solved.
"""
import copy

import pandas as pd
import pyomo.environ as pyo
import pytest

import data_prep.definitions_common as common
import model.components_common as cc
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model.reset_items import get_lists_and_dicts
from tests.backcast_data import INTO_CH, OUT_OF_CH, write_processed

NAME = "hist_market_obs"
PERIOD = "2025-10-01/2025-10-01"  # 24 hours from 2025-09-30 22:00 UTC, model steps t_6553 to t_6576


@pytest.fixture
def isolated_globals():
    lists, dicts = get_lists_and_dicts(common, [])
    saved = [(getattr(common, name), copy.deepcopy(getattr(common, name))) for name in lists + dicts]
    yield
    for container, content in saved:
        container.clear()
        container.update(content) if isinstance(container, dict) else container.extend(content)


@pytest.fixture
def folder(tmp_path):
    return write_processed(tmp_path / "processed", PERIOD)


@pytest.fixture
def scenario(monkeypatch, folder):
    def settings(**changes):
        values = {"CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": PERIOD,
                  "historical_data_dir": str(folder), **changes}
        monkeypatch.setitem(read_settings.scenarios_list, NAME, values)
        return read_settings.read_scenario_settings(NAME)
    return settings


def line_limits(staged):
    """The production line-limit rows of a small model, from the staged limits written into the globals."""
    hi.write_staged({name: staged[name] for name in ("ATC_exportlimit", "ATC_importlimit")})
    m = pyo.ConcreteModel()
    m.lineATC = pyo.Set(initialize=[f"HVAC_{n}_CH00" for n in hi.BORDER_NEIGHBOURS])
    m.T = pyo.Set(initialize=["t_6553", "t_6576"])
    m.Scenarios = pyo.Set(initialize=[NAME])
    m.ConstraintNames = pyo.Set(initialize=["lineATClimit"])
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=1.0)
    m.Export = pyo.Var(m.lineATC, m.T, m.Scenarios)
    m.lineATClimit = pyo.Constraint(m.lineATC, m.T, m.Scenarios, rule=cc.ATCbound_Constraint)
    return m.lineATClimit


@pytest.mark.parametrize("ratio", [0, 1, 2])
def test_bounds_follow_each_line_direction(scenario, isolated_globals, ratio):
    staged, _, _ = hi.read_market(NAME, scenario(NTC_CH_ratio=ratio))
    rows = line_limits(staged)
    for n in hi.BORDER_NEIGHBOURS:
        row = rows[f"HVAC_{n}_CH00", "t_6576", NAME]  # positive flow runs from the neighbour into CH00
        assert pyo.value(row.upper) == ratio * INTO_CH[n]
        assert pyo.value(row.lower) == -ratio * OUT_OF_CH[n]


def test_prices_map_each_hour_to_its_model_step_and_keep_negative_values(scenario):
    staged, files, assumptions = hi.read_market(NAME, scenario())
    prices = staged["Line_trade_price"]
    assert len(prices) == 4 * 24
    # the first hour of the period is the second row of the table: -5 + 1 for DE00, 50 + 1 for the others
    assert prices[("HVAC_DE00_CH00", "t_6553", NAME)] == -4.0
    assert prices[("HVAC_FR00_CH00", "t_6576", NAME)] == 50.0 + 24
    assert "'swissgrid_d2': 192" in assumptions[1] and len(files) == 2


def rewrite(folder, table, change):
    path = folder / table
    frame = change(pd.read_csv(path))
    frame.to_csv(path, index=False)


@pytest.mark.parametrize("table, change, message", [
    (hi.NTC_TABLE, lambda f: f.drop(index=5), "hours of the period are missing"),
    (hi.NTC_TABLE, lambda f: f.drop(columns="FR_to_CH"), r"no column \['FR_to_CH'\]"),
    (hi.PRICES_TABLE, lambda f: pd.concat([f, f.iloc[[3]]]), "duplicate hours"),
    (hi.NTC_TABLE, lambda f: f.assign(IT_to_CH=f["IT_to_CH"].where(f.index != 4)), "non-finite values of IT_to_CH"),
    (hi.NTC_TABLE, lambda f: f.assign(CH_to_AT=-1.0), "negative transfer limits"),
    (hi.PRICES_TABLE, lambda f: f.assign(AT00=float("inf")), "non-finite values of AT00"),
])
def test_input_errors_raise_naming_series_and_hours(scenario, folder, table, change, message):
    rewrite(folder, table, change)
    with pytest.raises(ValueError, match=message):
        hi.read_market(NAME, scenario())


def test_duplicate_border_column_raises(scenario, folder):
    path = folder / hi.NTC_TABLE
    lines = path.read_text().splitlines()
    lines[0] = lines[0].replace("CH_to_AT", "DE_to_CH")  # DE_to_CH now appears twice
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match="duplicate columns"):
        hi.read_market(NAME, scenario())


def test_missing_table_raises(scenario, folder):
    (folder / hi.PRICES_TABLE).unlink()
    with pytest.raises(FileNotFoundError, match=rf"{hi.PRICES_TABLE} not found \(historical_data_dir"):
        hi.read_market(NAME, scenario())


@pytest.mark.parametrize("ratio", [-1, float("nan"), "1.5", True])
def test_invalid_ntc_multiplier_raises(scenario, ratio):
    with pytest.raises(ValueError, match="NTC_CH_ratio"):
        hi.read_market(NAME, scenario(NTC_CH_ratio=ratio))


def test_market_inputs_are_no_longer_missing():
    assert "neighbour prices" not in hi.NOT_YET_BUILT and "transfer limits" not in hi.NOT_YET_BUILT
