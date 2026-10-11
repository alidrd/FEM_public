"""Unit tests for the fixed plants of a back-cast, nuclear and "others": read_fixed_plants and installed_capacity in
data_prep/historical_import.py, and add_fixed_generation in model/components_historical.py. The model tests build
small Pyomo models with the production rules (no solver).
"""
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
import pytest

import model.components_common as cc
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model import components_historical as ch
from model.constraint_scaling import constraint_scaling
from tests.backcast_data import write_processed

NAME = "hist_fixed_obs"
PERIOD = "2025-10-01/2025-10-01"  # 24 hours, model steps t_6553 to t_6576; row k of a table is hour k of the period
NEW_YEAR = "2024-12-31/2025-01-01"  # C(t) of others changes from 1300 MW (2024) to 1200 MW (2025) at local midnight
TOLERANCE = 1e-9


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(period=PERIOD):
        folder = write_processed(tmp_path / period.replace("/", "_"), period)
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": period,
            "historical_data_dir": str(folder)})
        return read_settings.read_scenario_settings(NAME)
    return make


def rewrite(settings, table, change):
    path = Path(settings["historical_data_dir"]) / table
    change(pd.read_csv(path)).to_csv(path, index=False)


def set_row(column, row, value):
    return lambda f: f.assign(**{column: f[column].where(f.index != row, value)})


# --- data side ----------------------------------------------------------------------------------------------------

def test_capacity_is_P_base_and_availability_is_observed_over_P_base(settings_for):
    staged, files, _ = hi.read_fixed_plants(NAME, settings_for())
    assert staged["Plant_capacity_gen"] == {("CH03_nuclear", NAME): 2973.0, ("CH01_other", NAME): 1200.0}
    assert staged["Avail_plant"][("CH03_nuclear", "t_6553", NAME)] == 2001.0 / 2973.0
    assert staged["Avail_plant"][("CH01_other", "t_6576", NAME)] == 324.0 / 1200.0
    assert staged["Map_plant_tech"] == {"CH03_nuclear": "nuclear", "CH01_other": "other"}
    assert staged["cost_data_opr_slp"] == {("nuclear", NAME): 0.0, ("other", NAME): 0.0}
    assert [Path(f).name for f in files] == [hi.POWER_TABLE, hi.NUCLEAR_TABLE, hi.INSTALLED_TABLE]


def test_nuclear_capacity_comes_from_the_nuclear_table_and_others_from_installed_power(settings_for):
    settings = settings_for()
    rewrite(settings, hi.INSTALLED_TABLE, lambda f: f.assign(nuclear=1.0, others=[1300.0, 1250.0]))
    staged = hi.read_fixed_plants(NAME, settings)[0]
    assert staged["Plant_capacity_gen"] == {("CH03_nuclear", NAME): 2973.0, ("CH01_other", NAME): 1250.0}


@pytest.mark.parametrize("period, years, first, last", [
    (NEW_YEAR, {2024: 2024, 2025: 2025}, 1300.0, 1200.0),
    ("2026-01-05/2026-01-05", {2026: 2025}, 1200.0, 1200.0),  # 2026 is not published: the latest year, 2025
])
def test_each_hour_takes_the_latest_published_year_not_after_its_local_year(settings_for, period, years, first, last):
    capacity, used = hi.installed_capacity(settings_for(period), "others")
    assert used == years and (capacity.iloc[0], capacity.iloc[-1]) == (first, last)
    if period == NEW_YEAR:  # the last local hour of 2024 is t_8760, the first of 2025 is t_1
        assert (capacity["t_8760"], capacity["t_1"]) == (1300.0, 1200.0)


def test_period_before_the_first_published_year_raises(settings_for):
    with pytest.raises(ValueError, match="no published year up to 2023"):
        hi.installed_capacity(settings_for("2023-12-01/2023-12-01"), "others")


@pytest.mark.parametrize("period, table, change, message", [
    (PERIOD, hi.POWER_TABLE, set_row("others", 5, 1250.0),
     r"CH01_other: 1 observed values above the capacity C\(t\), e.g. at t_6557 \(1250.0 > 1200.0 MW\)"),
    (PERIOD, hi.POWER_TABLE, set_row("nuclear", 3, 3000.0), "CH03_nuclear: 1 observed values above the capacity"),
    (NEW_YEAR, hi.POWER_TABLE, set_row("others", 30, 1250.0), "CH01_other: 1 observed values above the capacity C"),
    (PERIOD, hi.POWER_TABLE, set_row("nuclear", 2, -1.0), "CH03_nuclear: 1 negative observed values, e.g. at t_6554"),
    (PERIOD, hi.NUCLEAR_TABLE, lambda f: f.assign(net_capacity_MW=float("nan")), "non-finite values of net_capacity_MW"),
    (PERIOD, hi.INSTALLED_TABLE, lambda f: f.assign(others=[1300.0, float("inf")]), "non-finite values of others"),
    (PERIOD, hi.INSTALLED_TABLE, lambda f: pd.concat([f, f.iloc[[1]]]), r"duplicate years \[2025\]"),
])
def test_invalid_observed_or_capacity_raises(settings_for, period, table, change, message):
    settings = settings_for(period)
    rewrite(settings, table, change)
    with pytest.raises(ValueError, match=message):
        hi.read_fixed_plants(NAME, settings)


def test_crossing_period_accepts_a_value_above_the_later_capacity_in_an_hour_of_the_earlier_year(settings_for):
    settings = settings_for(NEW_YEAR)
    rewrite(settings, hi.POWER_TABLE, set_row("others", 20, 1250.0))  # a 2024 hour: C(t) is 1300 MW
    assert hi.read_fixed_plants(NAME, settings)[0]["Plant_capacity_gen"][("CH01_other", NAME)] == 1300.0


def test_zero_normalisation_capacity_raises(settings_for):
    settings = settings_for()
    rewrite(settings, hi.POWER_TABLE, lambda f: f.assign(others=0.0))
    rewrite(settings, hi.INSTALLED_TABLE, lambda f: f.assign(others=0.0))
    with pytest.raises(ValueError, match="CH01_other: the normalisation capacity P_base is 0.0, not positive"):
        hi.read_fixed_plants(NAME, settings)


def test_assumptions_record_series_capacities_and_the_year_rule(settings_for):
    assumptions = hi.read_fixed_plants(NAME, settings_for(NEW_YEAR))[2]
    text = " | ".join(assumptions)
    assert "CH03_nuclear = nuclear, CH01_other = others" in text and "P_base 2973.0 MW" in text
    assert "(local year -> published year {2024: 2024, 2025: 2025}); P_base 1300.0 MW" in text
    assert "not an hourly measurement" in text and "operating cost of nuclear, other: 0" in text


def test_fixed_plants_are_no_longer_missing():
    assert "fixed plants" not in hi.NOT_YET_BUILT


# --- model side ---------------------------------------------------------------------------------------------------

def small_model(staged, settings):
    """The fixed plants with the production generation limit and the back-cast's fixed generation."""
    m = pyo.ConcreteModel()
    m.P_gen = pyo.Set(initialize=list(hi.FIXED_PLANT_TECH))
    m.P_energylim = pyo.Set(initialize=[])
    m.P_fuellimCH = pyo.Set(initialize=[])
    m.T = pyo.Set(initialize=settings["T_list"], ordered=True)
    m.Scenarios = pyo.Set(initialize=[NAME])
    m.ConstraintNames = pyo.Set(initialize=["generation_limit"])
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=constraint_scaling["generation_limit"])
    m.gen_max = pyo.Var(m.P_gen, m.Scenarios)
    for (p, s), value in staged["Plant_capacity_gen"].items():
        m.gen_max[p, s].fix(value)
    m.avail_plant = pyo.Param(m.P_gen, m.T, m.Scenarios, initialize=staged["Avail_plant"])
    m.gen = pyo.Var(m.P_gen, m.T, m.Scenarios)
    m.generation_limit = pyo.Constraint(m.P_gen, m.T, m.Scenarios, rule=cc.generation_limit_Constraint)
    return m


def holds(row):
    body = pyo.value(row.body)
    return bool((row.lower is None or body >= pyo.value(row.lower) - TOLERANCE)
                and (row.upper is None or body <= pyo.value(row.upper) + TOLERANCE))


def test_output_equals_the_series_within_the_generation_limit_across_a_change_of_capacity(settings_for):
    settings = settings_for(NEW_YEAR)
    staged = hi.read_fixed_plants(NAME, settings)[0]
    observed = hi.read_processed_hourly(settings, hi.POWER_TABLE, ["nuclear", "others"])
    m = small_model(staged, settings)
    ch.add_fixed_generation(m)
    assert ch.registered_constraints(m) == ["fixed_generation"] and list(m.P_fixed) == list(hi.FIXED_PLANT_TECH)
    for p, column in hi.FIXED_PLANT_SERIES.items():
        for t in m.T:
            for offset, expected in ((0.0, True), (1.0, False), (-1.0, False)):
                m.gen[p, t, NAME].value = observed.at[t, column] + offset
                assert holds(m.fixed_generation[p, t, NAME]) is expected
            m.gen[p, t, NAME].value = observed.at[t, column]
            assert holds(m.generation_limit[p, t, NAME])


@pytest.mark.parametrize("change, message", [
    (lambda m: m.P_energylim.add("CH01_other"), "CH01_other has an energy limit"),
    (lambda m: m.P_fuellimCH.add("CH03_nuclear"), "CH03_nuclear has a fuel limit"),
    (lambda m: m.P_gen.remove("CH03_nuclear"), "CH03_nuclear is no generating plant"),
])
def test_energy_or_fuel_limit_or_a_missing_plant_raises(settings_for, change, message):
    settings = settings_for()
    m = small_model(hi.read_fixed_plants(NAME, settings)[0], settings)
    change(m)
    with pytest.raises(ValueError, match=message):
        ch.add_fixed_generation(m)


def test_hook_adds_fixed_generation_to_a_conforming_model(settings_for, monkeypatch, tmp_path):
    settings = settings_for()
    m = small_model(hi.read_fixed_plants(NAME, settings)[0], settings)
    m.P = pyo.Set(initialize=list(hi.FIXED_PLANT_TECH))
    m.PDH = pyo.Set(initialize=[])
    m.P_dsr = pyo.Set(initialize=[])
    for p, tech in hi.FIXED_PLANT_TECH.items():
        monkeypatch.setitem(ch.Map_plant_tech, p, tech)
        monkeypatch.setitem(ch.Map_plant_node, p, "CH00")
    presets = tmp_path / "presets.csv"
    presets.write_text("gen_max,other_meta,CH03_nuclear,other_meta_s,1\n")
    ch.apply_historical_constraints(m, "hist_fixed", presets)
    assert ch.registered_constraints(m) == ["fixed_generation"] and len(m.fixed_generation) == 2 * 24


def test_fixed_plants_belong_to_the_backcast_fleet(monkeypatch):
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=list(hi.FIXED_PLANT_TECH))
    m.PDH = pyo.Set(initialize=[])
    for p, tech in hi.FIXED_PLANT_TECH.items():
        monkeypatch.setitem(ch.Map_plant_tech, p, tech)
        monkeypatch.setitem(ch.Map_plant_node, p, "CH00")
    assert ch.plant_problems(m) == []
