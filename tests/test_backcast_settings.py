"""Unit tests for the back-cast switch, its time base and its setting checks (data_prep/historical_import.py,
model/read_settings.py and the switch in data_prep/data_import_TYNDP.py). No model is built.
"""
import pandas as pd
import pytest

import model.read_settings as read_settings
from data_prep import historical_import as hi
from scenarios.settings_default import seetings_default_param

FIRST_RUN = "2025-10-01/2026-08-30"


def backcast_settings(**changes):
    settings = {**seetings_default_param, "CH_only": True, "allow_investment": False, "NodeDH_list": [],
                "historical_period": FIRST_RUN, "historical_data_dir": "input/historical/processed/x"}
    settings.update(changes)
    return settings


# --- period and model steps ---------------------------------------------------------------------------

def test_period_covers_its_local_days_in_utc():
    start, end = hi.period_bounds_utc(FIRST_RUN)
    assert start == pd.Timestamp("2025-09-30 22:00", tz="UTC")
    assert end == pd.Timestamp("2026-08-30 22:00", tz="UTC")
    assert (end - start) / pd.Timedelta(hours=1) == 8016


@pytest.mark.parametrize("period", [FIRST_RUN, "2024-10-01/2025-08-30"])
def test_model_steps_use_a_365_day_reference_calendar(period):
    # 2024 is a leap year, but this period does not contain 29 February
    assert hi.model_steps(period) == (6553, 5808)


def test_time_map_wraps_and_is_one_to_one():
    time_map = hi.historical_time_map("2025-10-01/2026-09-30")
    assert len(time_map) == 8760
    assert time_map["t"].iloc[0] == "t_6553" and time_map["t"].iloc[2208] == "t_1"
    assert set(time_map["t"]) == {f"t_{k}" for k in range(1, 8761)}
    assert (time_map["time_utc"].diff().dropna() == pd.Timedelta(hours=1)).all()


@pytest.mark.parametrize("day, hours", [("2025-10-26", 25), ("2026-03-29", 23)])
def test_dst_days_map_their_consecutive_utc_hours(day, hours):
    time_map = hi.historical_time_map(f"{day}/{day}")
    assert len(time_map) == hours
    assert (time_map["time_utc"].diff().dropna() == pd.Timedelta(hours=1)).all()
    assert time_map["time_local"].iloc[0].hour == 0 and time_map["time_local"].iloc[-1].hour == 23


def test_period_containing_29_february_raises():
    with pytest.raises(ValueError, match="29 February"):
        hi.period_bounds_utc("2024-02-01/2024-03-31")


def test_period_over_8760_hours_raises():
    with pytest.raises(ValueError, match="8760"):
        hi.period_bounds_utc("2025-03-01/2026-03-01")


@pytest.mark.parametrize("value", [1981.99, "nan", "2025-10-01", "2025-13-01/2026-01-01", "2026-08-30/2025-10-01"])
def test_malformed_reversed_or_invalid_period_raises(value):
    with pytest.raises(ValueError, match="historical_period"):
        hi.period_bounds_utc(value)


# --- setting checks -----------------------------------------------------------------------------------

def test_consistent_backcast_settings_pass():
    hi.check_backcast_settings("s", backcast_settings())


@pytest.mark.parametrize("setting, value", [
    ("CH_only", False),
    ("allow_investment", True),
    ("rep_hydro_plants", False),
    ("Node_list_setting", ["CH00", "CH01", "DE00"]),
    ("hydro_inflow_TWh", 20.06),
    ("ror_annual_TWh", 18.55),
    ("pump_capacity_GW", 5.54),
    ("adding_hydro_storage_cap_TWh", 2),
    ("reduce_inflex_demand_by_[MWh]", -5659884),
    ("slack_soc", True),
    ("minimum_RES_target_CH", 1e6),
    ("winter_limit", {"mode": "absolute", "window": [6553, 2184], "energy_MWh": 5e6}),
    ("NodeDH_list", ["DH_medium"]),
    ("historical_data_dir", False),
])
def test_each_conflicting_setting_raises_naming_it(setting, value):
    with pytest.raises(ValueError, match=setting.replace("[", r"\[").replace("]", r"\]")):
        hi.check_backcast_settings("s", backcast_settings(**{setting: value}))


def test_backcast_meta_scenario_needs_exactly_one_sub_scenario():
    hi.check_backcast_meta_scenario("m", {"m_a": backcast_settings()})
    hi.check_backcast_meta_scenario("m", {"m_a": seetings_default_param, "m_b": seetings_default_param})
    with pytest.raises(ValueError, match="one sub-scenario"):
        hi.check_backcast_meta_scenario("m", {"m_a": backcast_settings(), "m_b": backcast_settings()})
    with pytest.raises(ValueError, match="one sub-scenario"):
        hi.check_backcast_meta_scenario("m", {"m_a": backcast_settings(), "m_b": seetings_default_param})


# --- read_settings and the import switch --------------------------------------------------------------

@pytest.fixture
def scenario(monkeypatch):
    def add(name, **csv_values):
        monkeypatch.setitem(read_settings.scenarios_list, name, csv_values)
        return name
    return add


def test_backcast_settings_derive_the_model_steps(scenario):
    name = scenario("hist_s", **{k: v for k, v in backcast_settings().items() if k not in seetings_default_param
                                 or backcast_settings()[k] != seetings_default_param[k]})
    settings = read_settings.read_scenario_settings(name)
    assert (settings["t_start"], settings["t_end"]) == (6553, 5808)
    assert len(settings["T_list"]) == 8016 and settings["T_list"][0] == "t_6553" and settings["T_list"][-1] == "t_5808"


def test_t_end_in_the_scenario_csv_must_agree_with_the_period(scenario):
    name = scenario("hist_s", CH_only=True, allow_investment=False, NodeDH_list=[], historical_period=FIRST_RUN,
                    historical_data_dir="x", t_end=6552)
    with pytest.raises(ValueError, match="t_end"):
        read_settings.read_scenario_settings(name)


def test_settings_without_historical_period_are_unchanged(scenario):
    settings = read_settings.read_scenario_settings(scenario("normal_s"))
    assert (settings["t_start"], settings["t_end"]) == (6553, 6552) and len(settings["T_list"]) == 8760


def test_backcast_import_stops_until_it_is_built(scenario):
    from data_prep.data_import_TYNDP import data_import_TYNDP_fcn

    name = scenario("hist_s", CH_only=True, allow_investment=False, NodeDH_list=[], historical_period=FIRST_RUN,
                    historical_data_dir="x")
    with pytest.raises(NotImplementedError, match="back-cast inputs not built yet"):
        data_import_TYNDP_fcn(name)
