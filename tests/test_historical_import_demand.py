"""Unit tests for the Swiss demand of a back-cast (read_demand in data_prep/historical_import.py). No model is
solved.
"""
from pathlib import Path

import pandas as pd
import pytest

import model.read_settings as read_settings
from data_prep import historical_import as hi
from tests.backcast_data import write_processed

NAME = "hist_demand_obs"
PERIOD = "2025-10-01/2025-10-01"  # 24 hours, model steps t_6553 to t_6576; row k of the table is hour k of the period


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


def rewrite(folder, change):
    path = folder / hi.DEMAND_TABLE
    change(pd.read_csv(path)).to_csv(path, index=False)


@pytest.mark.parametrize("column, offset", [("demand_MW", 0), ("demand_solar_shaped_MW", -100),
                                            ("demand_flat_MW", 100)])
def test_fixed_consumer_demand_is_the_chosen_column_at_every_model_step(scenario, column, offset):
    settings = scenario(historical_demand=column)
    staged, files, _ = hi.read_demand(NAME, settings)
    expected = {(hi.CONSUMER, "fixed", t, NAME): 6000.0 + k + offset for k, t in enumerate(settings["T_list"], 1)}
    assert staged == {"Demand_data": expected}
    assert files == [str(Path(settings["historical_data_dir"]) / hi.DEMAND_TABLE)]


def test_default_column_is_demand_MW(scenario):
    staged, _, assumptions = hi.read_demand(NAME, scenario())
    assert staged["Demand_data"][(hi.CONSUMER, "fixed", "t_6553", NAME)] == 6001.0
    assert "column demand_MW" in assumptions[0] and "placed a share 0.5 (onsite_solar_share_assumed)" in assumptions[0]


@pytest.mark.parametrize("column", ["swissgrid_end_user_MW", "demand", False])
def test_unknown_column_raises(scenario, column):
    with pytest.raises(ValueError, match="historical_demand must be one of"):
        hi.read_demand(NAME, scenario(historical_demand=column))


def test_nan_inside_the_period_raises_naming_the_hours(scenario, folder):
    rewrite(folder, lambda f: f.assign(demand_MW=f["demand_MW"].where(~f.index.isin([4, 7]))))
    with pytest.raises(ValueError, match=r"2 missing or non-finite values of demand_MW, e.g. at 2025-10-01 01:00"):
        hi.read_demand(NAME, scenario())


def test_nan_outside_the_period_or_in_another_column_is_not_read(scenario, folder):
    rewrite(folder, lambda f: f.assign(demand_MW=f["demand_MW"].where(f.index != 0), demand_flat_MW=float("nan")))
    staged, _, _ = hi.read_demand(NAME, scenario())
    assert len(staged["Demand_data"]) == 24


def test_negative_demand_raises(scenario, folder):
    rewrite(folder, lambda f: f.assign(demand_MW=f["demand_MW"].where(f.index != 3, -1.0)))
    with pytest.raises(ValueError, match="1 negative values of demand_MW, e.g. at t_6555"):
        hi.read_demand(NAME, scenario())


def test_on_site_share_varying_within_the_period_raises(scenario, folder):
    rewrite(folder, lambda f: f.assign(onsite_solar_share_assumed=[0.5] * 10 + [0.6] * (len(f) - 10)))
    with pytest.raises(ValueError, match="onsite_solar_share_assumed varies"):
        hi.read_demand(NAME, scenario())


@pytest.mark.parametrize("column, placement", [("demand_solar_shaped_MW", "all like PV output"),
                                               ("demand_flat_MW", "all flat")])
def test_assumptions_record_the_column_its_placement_and_provisional_months(scenario, folder, column, placement):
    rewrite(folder, lambda f: f.assign(bfe_month_definitive=[1.0] * 20 + [0.0] * (len(f) - 20)))
    _, _, assumptions = hi.read_demand(NAME, scenario(historical_demand=column))
    assert f"column {column}" in assumptions[0] and assumptions[0].endswith(f"placed {placement}")
    assert "5 of 24 hours" in assumptions[1]  # rows 20 to 24 lie in the period, row 25 after it


def test_swiss_demand_is_no_longer_missing():
    assert "Swiss demand" not in hi.NOT_YET_BUILT
