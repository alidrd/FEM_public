"""Unit tests for the reservoir boundaries of a back-cast: read_reservoir_stocks, fill_readings and fill_at in
data_prep/historical_import.py, and add_reservoir_boundaries in model/components_historical.py. The model tests use
the production storage balance and start condition on small Pyomo models, and the production dual exporter (no
solver).
"""
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
import pytest
from pyomo.repn import generate_standard_repn

import aggregation.results_export as res_export
import model.components_common as cc
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model import components_historical as ch
from tests.backcast_data import write_processed

NAME = "hist_reservoir_obs"
# 2025-09-30 22:00 to 2025-10-05 22:00 UTC: the start lies 48 hours after the reading of Monday 29 September
# (2025-09-28 22:00 UTC), the end is the reading of Monday 6 October (2025-10-05 22:00 UTC)
PERIOD = "2025-10-01/2025-10-05"
OPEN_STORAGE = {"large_psp": 8647500.0, "medium_reservior": 151400.0, "small_reservior": 1200.0}
TOLERANCE = 1e-9


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(period=PERIOD, **changes):
        folder = write_processed(tmp_path / period.replace("/", "_"), period)
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": period,
            "historical_data_dir": str(folder), **changes})
        return read_settings.read_scenario_settings(NAME)
    return make


def fleet(settings):
    return hi.read_hydro_fleet(NAME, settings)[0]


def readings(settings):
    table = pd.read_csv(Path(settings["historical_data_dir"]) / hi.RESERVOIR_TABLE)
    return table.assign(fill=table["TotalCH_GWh"] / table["TotalCH_max_GWh"]).set_index("date")["fill"]


def rewrite(settings, change):
    path = Path(settings["historical_data_dir"]) / hi.RESERVOIR_TABLE
    change(pd.read_csv(path)).to_csv(path, index=False)


# --- data side ----------------------------------------------------------------------------------------------------

def test_monday_reading_is_sunday_24_local_in_utc():
    table = pd.DataFrame({"date": ["2025-09-29", "2025-12-01"], "TotalCH_GWh": [7600.0, 6000.0],
                          "TotalCH_max_GWh": 8895.0})
    instants = hi.fill_readings(table).index
    assert list(instants) == [pd.Timestamp("2025-09-28 22:00", tz="UTC"), pd.Timestamp("2025-11-30 23:00", tz="UTC")]


def test_start_stock_lies_before_the_first_hour_and_the_end_target_after_the_last(settings_for):
    settings = settings_for()
    staged, files, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    hours = hi.historical_time_map(PERIOD)["time_utc"]
    fill = readings(settings)
    start_fill = fill["2025-09-29"] + (fill["2025-10-06"] - fill["2025-09-29"]) * 48 / 168
    for p, storage in OPEN_STORAGE.items():  # equal fill fraction for every open plant
        assert staged["Reservoir_start_stock"][(p, NAME)] == pytest.approx(start_fill * storage, rel=TOLERANCE)
    assert staged["Reservoir_end_target"] == {NAME: {"mode": "equal", "MWh": pytest.approx(
        fill["2025-10-06"] * sum(OPEN_STORAGE.values()), rel=TOLERANCE)}}
    assert f"start stock at {hours.iloc[0]} (before the first hour)" in assumptions[1]
    assert f"end target at {hours.iloc[-1] + pd.Timedelta(hours=1)} (after the last hour)" in assumptions[2]
    assert "reading at 2025-10-05 22:00:00+00:00" in assumptions[2]  # an exact reading is used directly
    assert [Path(f).name for f in files] == [hi.RESERVOIR_TABLE]


def test_start_and_end_at_the_same_offset_in_their_weeks_are_interpolated_separately(settings_for):
    settings = settings_for("2025-10-01/2025-10-07")  # both instants lie 48 hours after a reading
    staged = hi.read_reservoir_stocks(NAME, settings, fleet(settings))[0]
    fill = readings(settings)
    start = fill["2025-09-29"] + (fill["2025-10-06"] - fill["2025-09-29"]) * 48 / 168
    end = fill["2025-10-06"] + (fill["2025-10-13"] - fill["2025-10-06"]) * 48 / 168
    assert start != end
    assert staged["Reservoir_start_stock"][("large_psp", NAME)] == pytest.approx(start * 8647500.0, rel=TOLERANCE)
    assert staged["Reservoir_end_target"][NAME]["MWh"] == pytest.approx(end * sum(OPEN_STORAGE.values()), rel=TOLERANCE)


@pytest.mark.parametrize("change, message", [
    (lambda f: f[f["date"] != "2025-10-06"], "more than 8 days apart"),
    (lambda f: f.assign(TotalCH_max_GWh=f["TotalCH_max_GWh"].where(f["date"] != "2025-10-13", 0.0)),
     r"TotalCH_max_GWh is not positive on \['2025-10-13'\]"),
    (lambda f: f[f["date"] >= "2025-10-06"],
     "no reading before 2025-09-30 22:00:00\\+00:00; stocks are not extrapolated"),
    (lambda f: f[f["date"] <= "2025-09-29"], "no reading after"),
    (lambda f: pd.concat([f, f.iloc[[1]]]), "duplicate dates"),
])
def test_invalid_readings_raise(settings_for, change, message):
    settings = settings_for()
    rewrite(settings, change)
    with pytest.raises(ValueError, match=message):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


def test_at_least_keeps_the_end_target(settings_for):
    settings = settings_for(historical_hydro_end="at_least")
    staged = hi.read_reservoir_stocks(NAME, settings, fleet(settings))[0]
    assert staged["Reservoir_end_target"] == {NAME: {"mode": "at_least", "MWh": pytest.approx(
        readings(settings)["2025-10-06"] * sum(OPEN_STORAGE.values()), rel=TOLERANCE)}}


@pytest.mark.parametrize("mode, raises", [("equal", True), ("free", False)])
def test_free_end_needs_no_reading_after_the_end(settings_for, mode, raises):
    settings = settings_for("2025-10-01/2025-10-08", historical_hydro_end=mode)  # ends between two readings
    rewrite(settings, lambda f: f[f["date"] <= "2025-10-06"])
    if raises:
        with pytest.raises(ValueError, match="no reading after 2025-10-08 22:00:00"):
            hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    else:
        staged, _, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
        assert staged["Reservoir_end_target"] == {NAME: {"mode": "free", "MWh": None}}
        assert assumptions[2].startswith("end target: none")


def test_unknown_end_mode_raises(settings_for):
    settings = settings_for(historical_hydro_end="equals")
    with pytest.raises(ValueError, match="historical_hydro_end must be one of"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


def test_every_input_is_built():
    assert hi.NOT_YET_BUILT == []


# --- model side ---------------------------------------------------------------------------------------------------

SF = 1e-3  # a non-unit scale of the storage balance
STORAGE = {"big": 1000.0, "small": 100.0, "loop": 50.0}  # two open plants of unequal capacity and a closed loop


@pytest.fixture
def boundaries(monkeypatch):
    def set_targets(mode="equal", target=620.0):
        monkeypatch.setitem(ch.Reservoir_start_stock, ("big", "s"), 400.0)
        monkeypatch.setitem(ch.Reservoir_start_stock, ("small", "s"), 40.0)
        monkeypatch.setitem(ch.Reservoir_end_target, "s", {"mode": mode, "MWh": None if mode == "free" else target})
    return set_targets


def storage_model():
    """Three storage plants over three cyclic steps with the production balance and start condition."""
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=list(STORAGE))
    m.P_storage = pyo.Set(initialize=list(STORAGE))
    m.P_pumping = pyo.Set(initialize=["big", "loop"])
    m.P_gen = pyo.Set(initialize=list(STORAGE))
    m.P_hydro = pyo.Set(initialize=list(STORAGE))
    m.P_inflow = pyo.Set(initialize=["big", "small"])
    m.P_outflow = pyo.Set(initialize=[])
    m.P_ev = pyo.Set(initialize=[])
    m.T = pyo.Set(initialize=["t_1", "t_2", "t_3"], ordered=True)
    m.Scenarios = pyo.Set(initialize=["s"])
    m.ConstraintNames = pyo.Set(initialize=["storage_soc", "storage_start_condition"])
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=SF)
    # electrical basis for the open plants, workbook values for the closed loop
    m.storage_charge_eff_in = pyo.Param(m.P_storage, m.Scenarios,
                                        initialize={("big", "s"): 0.7569, ("small", "s"): 0.7569, ("loop", "s"): 0.87})
    m.storage_charge_eff_out = pyo.Param(m.P_storage, m.Scenarios,
                                         initialize={("big", "s"): 1.0, ("small", "s"): 1.0, ("loop", "s"): 0.87})
    m.inflow = pyo.Param(m.P_inflow, m.T, m.Scenarios, initialize=30.0)
    m.storage_start_cond = pyo.Param(m.P_storage, m.Scenarios, initialize=0.95)
    m.gen_energy_max = pyo.Var(m.P, m.Scenarios)
    for p, capacity in STORAGE.items():
        m.gen_energy_max[p, "s"].fix(capacity)
    for name in ("soc", "storage_charge", "gen", "spill_water"):
        m.add_component(name, pyo.Var(m.P, m.T, m.Scenarios, initialize=0.0))
    m.storage_soc = pyo.Constraint(m.P_storage, m.T, m.Scenarios, rule=cc.storeBalance_Constraint)
    m.storage_start_condition = pyo.Constraint(m.P_storage, m.Scenarios, rule=cc.storage_start_condition_Constraint)
    return m


def holds(row):
    body = pyo.value(row.body)
    return bool((row.lower is None or body >= pyo.value(row.lower) - TOLERANCE)
                and (row.upper is None or body <= pyo.value(row.upper) + TOLERANCE))


@pytest.mark.parametrize("mode, final, accepted", [
    ("equal", (600.0, 20.0), True),  # fill fractions 0.6 and 0.2, sum 620 = target
    ("equal", (600.0, 21.0), False),
    ("equal", (650.0, 20.0), False),
    ("at_least", (600.0, 20.0), True),
    ("at_least", (650.0, 20.0), True),
    ("at_least", (590.0, 20.0), False),
])
def test_national_end_target(boundaries, mode, final, accepted):
    boundaries(mode)
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    m.soc["big", "t_3", "s"].value, m.soc["small", "t_3", "s"].value = final
    assert holds(m.reservoir_end_target["s"]) is accepted
    assert ch.registered_constraints(m) == ["reservoir_first_hour", "reservoir_end_target"]


def test_free_end_adds_no_target(boundaries):
    boundaries("free")
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    assert m.component("reservoir_end_target") is None and ch.registered_constraints(m) == ["reservoir_first_hour"]


def coefficients(row):
    repn = generate_standard_repn(row.body, compute_values=True)
    return {var.name: coef for var, coef in zip(repn.linear_vars, repn.linear_coefs)}, repn.constant


def test_first_hour_has_the_production_coefficients_with_the_start_stock_before_the_hour(boundaries):
    boundaries()
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    production, production_constant = coefficients(m.storage_soc["big", "t_1", "s"])  # deactivated, still built
    first, first_constant = coefficients(m.reservoir_first_hour["big", "s"])
    previous = m.soc["big", "t_3", "s"].name  # the cyclic link of the first hour
    assert set(first) == set(production) - {previous}
    assert {"storage_charge[big,t_1,s]", "gen[big,t_1,s]", "spill_water[big,t_1,s]"} <= set(first)
    for var, coef in first.items():  # unscaled: the production row divided by its scale
        assert coef * SF == pytest.approx(production[var], rel=TOLERANCE)
    assert first["soc[big,t_1,s]"] == 1.0
    # inflow (30) on the right side; the start stock replaces the previous stock
    assert first_constant == pytest.approx(-(400.0 + 30.0)) and production_constant == pytest.approx(-30.0 * SF)


def test_only_the_first_rows_of_the_open_plants_change(boundaries):
    boundaries()
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    for p in ("big", "small"):
        assert not m.storage_soc[p, "t_1", "s"].active and not m.storage_start_condition[p, "s"].active
        assert m.storage_soc[p, "t_2", "s"].active and m.storage_soc[p, "t_3", "s"].active
    assert all(m.storage_soc["loop", t, "s"].active for t in m.T) and m.storage_start_condition["loop", "s"].active


def test_first_hour_and_end_target_duals_are_exported_in_EUR_per_MWh(boundaries, tmp_path, monkeypatch):
    boundaries()
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    for row in m.storage_soc.values():
        if row.active:
            m.dual[row] = 2.0 / SF  # the solver dual of a scaled row
    m.dual[m.reservoir_first_hour["big", "s"]] = -55.0
    m.dual[m.reservoir_first_hour["small", "s"]] = -54.0
    m.dual[m.reservoir_end_target["s"]] = 61.5
    monkeypatch.chdir(tmp_path)
    res_export.constraints([m.storage_soc, m.reservoir_first_hour, m.reservoir_end_target], "run", m)
    folder = tmp_path / "output" / "run"
    assert pd.read_csv(folder / "reservoir_first_hour_dual.csv")["value"].tolist() == [-55.0, -54.0]
    assert pd.read_csv(folder / "reservoir_end_target_dual.csv")["value"].tolist() == [61.5]
    storage = pd.read_csv(folder / "storage_soc_dual.csv")
    assert storage["value"].tolist() == pytest.approx([2.0] * (9 - 2))  # the first rows of big and small are left out


@pytest.mark.parametrize("change, message", [
    (lambda mp: mp.setattr(ch, "Reservoir_start_stock", {}), "no start stock is staged"),
    (lambda mp: mp.setitem(ch.Reservoir_start_stock, ("loop", "s"), 10.0), "loop is no storage plant with inflow"),
    (lambda mp: mp.setitem(ch.Reservoir_end_target, "s", {"mode": "equals", "MWh": 1.0}), "no valid end target for s"),
])
def test_missing_or_invalid_boundaries_raise(boundaries, monkeypatch, change, message):
    boundaries()
    change(monkeypatch)
    with pytest.raises(ValueError, match=message):
        ch.add_reservoir_boundaries(storage_model())


def test_hook_adds_the_reservoir_boundaries(boundaries, monkeypatch):
    boundaries()
    monkeypatch.setattr(ch, "check_historical_model", lambda model, scenario_name, preset_file: None)
    monkeypatch.setattr(ch, "add_fixed_generation", lambda model: None)
    m = storage_model()
    ch.apply_historical_constraints(m, "hist", "unused.csv")
    assert ch.registered_constraints(m) == ["reservoir_first_hour", "reservoir_end_target"]
