"""Unit tests for the renewable infeed of a back-cast (read_infeed in data_prep/historical_import.py). No model is
solved.
"""
from pathlib import Path

import pandas as pd
import pytest

import model.read_settings as read_settings
from data_prep import historical_import as hi
from model.structural_parameters import tech_infeed_all_list
from tests.backcast_data import GENERATION_TABLE, write_processed

NAME = "hist_infeed_obs"
PERIOD = "2025-10-01/2025-10-01"  # 24 hours, model steps t_6553 to t_6576; row k of the table is hour k of the period
OBSERVED = {"pvrf": 100.0, "windon": 10.0, "ror": 1000.0}  # value of row 0 in tests/backcast_data.py


@pytest.fixture
def folder(tmp_path):
    return write_processed(tmp_path / "processed", PERIOD)


@pytest.fixture
def settings(monkeypatch, folder):
    monkeypatch.setitem(read_settings.scenarios_list, NAME, {
        "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": PERIOD,
        "historical_data_dir": str(folder)})
    return read_settings.read_scenario_settings(NAME)


def rewrite(folder, change):
    path = folder / GENERATION_TABLE
    change(pd.read_csv(path)).to_csv(path, index=False)


def test_infeed_is_the_observed_series_at_every_model_step(settings):
    staged, files, _ = hi.read_infeed(NAME, settings)
    infeed = staged["Infeed_consumers"]
    for tech, start in OBSERVED.items():
        assert [infeed[(hi.CONSUMER, tech, t, NAME)] for t in settings["T_list"]] == [start + k for k in range(1, 25)]
    assert files == [str(Path(settings["historical_data_dir"]) / GENERATION_TABLE)]


def test_every_other_infeed_technology_is_zero_at_every_model_step(settings):
    infeed = hi.read_infeed(NAME, settings)[0]["Infeed_consumers"]
    assert set(infeed) == {(hi.CONSUMER, tech, t, NAME) for tech in tech_infeed_all_list for t in settings["T_list"]}
    others = {value for (_, tech, _, _), value in infeed.items() if tech not in OBSERVED}
    assert others == {0.0} and set(tech_infeed_all_list) - set(OBSERVED) == {"windof", "pv"}


@pytest.mark.parametrize("column, value, message", [
    ("solar", float("nan"), "1 missing or non-finite values of solar"),
    ("wind_onshore", float("inf"), "1 missing or non-finite values of wind_onshore"),
    ("hydro_run_of_river", -1.0, r"negative generation in \['hydro_run_of_river'\]"),
])
def test_missing_non_finite_or_negative_value_raises(settings, folder, column, value, message):
    rewrite(folder, lambda f: f.assign(**{column: f[column].where(f.index != 6, value)}))
    with pytest.raises(ValueError, match=message):
        hi.read_infeed(NAME, settings)


def test_missing_column_raises(settings, folder):
    rewrite(folder, lambda f: f.drop(columns="wind_onshore"))
    with pytest.raises(ValueError, match=r"no column \['wind_onshore'\]"):
        hi.read_infeed(NAME, settings)


def test_assumptions_record_columns_energy_and_zero_technologies(settings):
    assumptions = hi.read_infeed(NAME, settings)[2]
    assert "solar as pvrf, wind_onshore as windon, hydro_run_of_river as ror" in assumptions[0]
    assert "ror 0.024 TWh" in assumptions[0]  # 1001 + ... + 1024 MWh
    assert assumptions[1].startswith("infeed of windof, pv: 0")


def test_renewable_infeed_is_no_longer_missing():
    assert "renewable infeed" not in hi.NOT_YET_BUILT


# --- historical_generation_source ------------------------------------------------------------------------------

def test_default_source_is_entsoe_profiles_at_bfe_months(settings):
    assert settings["historical_generation_source"] == "entsoe_bfe_months"
    assert [Path(f).name for f in hi.read_infeed(NAME, settings)[1]] == ["ch_generation_entsoe_bfe_hourly.csv"]


@pytest.mark.parametrize("source", ["energy_charts", "entsoe"])
def test_generation_source_selects_its_table_for_infeed_and_fixed_plants(monkeypatch, folder, source):
    table = hi.GENERATION_TABLES[source][0]
    default = pd.read_csv(folder / GENERATION_TABLE)
    doubled = {c: 2 * default[c] for c in ["solar", "wind_onshore", "hydro_run_of_river"]}
    plus_one = {c: default[c] + 1 for c in ["nuclear", "others"]}  # doubled, they would exceed their capacity
    default.assign(**doubled, **plus_one).to_csv(folder / table, index=False)
    monkeypatch.setitem(read_settings.scenarios_list, NAME, {
        "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": PERIOD,
        "historical_data_dir": str(folder), "historical_generation_source": source})
    settings = read_settings.read_scenario_settings(NAME)
    staged, files, assumptions = hi.read_infeed(NAME, settings)
    for tech, start in OBSERVED.items():
        assert [staged["Infeed_consumers"][(hi.CONSUMER, tech, t, NAME)] for t in settings["T_list"]] == \
               [2 * (start + k) for k in range(1, 25)]
    assert files == [str(folder / table)] and table in assumptions[0]
    fixed, fixed_files, _ = hi.read_fixed_plants(NAME, settings)
    assert Path(fixed_files[0]).name == table
    first = settings["T_list"][0]
    p_base = fixed["Plant_capacity_gen"][("CH03_nuclear", NAME)]
    assert fixed["Avail_plant"][("CH03_nuclear", first, NAME)] * p_base == pytest.approx(2001.0 + 1)


def test_unknown_generation_source_raises(monkeypatch, folder):
    monkeypatch.setitem(read_settings.scenarios_list, NAME, {
        "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": PERIOD,
        "historical_data_dir": str(folder), "historical_generation_source": "smard"})
    settings = read_settings.read_scenario_settings(NAME)
    with pytest.raises(ValueError, match="historical_generation_source must be one of"):
        hi.read_infeed(NAME, settings)
