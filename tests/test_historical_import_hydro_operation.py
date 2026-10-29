"""Unit tests for the hydro operating constraints of a back-cast (docs/backcast.md, section Storage-hydro operation):
read_hydro_operation and read_outages in data_prep/historical_import.py, and add_hydro_operation in
model/components_historical.py. The model tests use small Pyomo models and the production dual exporter (no solver).
"""
import math
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
import pytest
from pyomo.repn import generate_standard_repn

import aggregation.results_export as res_export
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model import components_historical as ch
from tests.backcast_data import write_processed

NAME = "hist_operation_obs"
# 2025-10-19 22:00 to 2025-10-31 23:00 UTC; the clocks go back on Sunday 26 October
PERIOD = "2025-10-20/2025-10-31"
TOLERANCE = 1e-9
COLUMNS = ["plant", "start_local", "end_local", "turbine_MW", "pump_MW", "source"]


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(period=PERIOD, **changes):
        folder = write_processed(tmp_path / period.replace("/", "_"), period)
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": period,
            "historical_data_dir": str(folder), **changes})
        return read_settings.read_scenario_settings(NAME)
    return make


def outage_file(tmp_path, rows, columns=COLUMNS):
    path = tmp_path / "outages.csv"
    pd.DataFrame(rows, columns=columns).to_csv(path, index=False)
    return str(path)


def read(settings):
    fleet = hi.read_hydro_fleet(NAME, settings)[0]
    return hi.read_hydro_operation(NAME, settings, fleet)


def steps_between(period, first_utc, end_utc):
    time_map = hi.historical_time_map(period)
    inside = (time_map["time_utc"] >= pd.Timestamp(first_utc, tz="UTC")) & (time_map["time_utc"] < pd.Timestamp(end_utc, tz="UTC"))
    return list(time_map.loc[inside, "t"])


# --- data side: settings ------------------------------------------------------------------------------------------

def test_floor_and_reserve_are_staged_per_scenario(settings_for):
    staged, files, assumptions = read(settings_for(historical_hydro_floor_MW=375, historical_hydro_reserve_up_MW=922))
    assert staged == {"Hydro_operation": {NAME: {"floor_MW": 375.0, "reserve_up_MW": 922.0}}, "Hydro_outage_cap": {}}
    assert files == []
    assert any(a.startswith("must-run floor: storage-hydro output") and "375.0 MW" in a for a in assumptions)
    assert any(a.startswith("upward reserve withheld") and "922.0 MW" in a for a in assumptions)


def test_everything_is_off_by_default(settings_for):
    staged, files, assumptions = read(settings_for())
    assert staged == {"Hydro_operation": {NAME: {"floor_MW": None, "reserve_up_MW": None}}, "Hydro_outage_cap": {}}
    assert files == [] and assumptions == []


@pytest.mark.parametrize("name", ["historical_hydro_floor_MW", "historical_hydro_reserve_up_MW"])
@pytest.mark.parametrize("value", ["375", True, -5, 0, math.nan, math.inf])
def test_floor_and_reserve_must_be_false_or_a_positive_number(settings_for, name, value):
    with pytest.raises(ValueError, match=f"{name} must be False or a positive finite number of MW"):
        read(settings_for(**{name: value}))


# --- data side: outages ---------------------------------------------------------------------------------------------

def test_an_outage_window_is_local_wall_time_end_exclusive_across_the_clock_change(settings_for, tmp_path):
    path = outage_file(tmp_path, [("large_psp", "2025-10-25 00:00", "2025-10-27 00:00", 4000, 1000, "test")])
    staged, files, assumptions = read(settings_for(historical_hydro_outages=path))
    # 25 October 00:00 CEST = 24 October 22:00 UTC; 27 October 00:00 CET = 26 October 23:00 UTC: 49 hours
    steps = steps_between(PERIOD, "2025-10-24 22:00", "2025-10-26 23:00")
    assert len(steps) == 49
    assert staged["Hydro_outage_cap"] == {("large_psp", t, NAME): (4000.0, 1000.0) for t in steps}
    assert files == [path]
    assert any("large_psp: 1 window, 49 of 49 hours in the period, turbines 4000.0 MW, pumps 1000.0 MW (test)" in a
               for a in assumptions)


def test_a_window_reaching_before_the_period_is_clipped(settings_for, tmp_path):
    path = outage_file(tmp_path, [("CH00_psp_close", "2025-10-15 00:00", "2025-10-21 00:00", 0, 0, "test")])
    staged, _, assumptions = read(settings_for(historical_hydro_outages=path))
    steps = steps_between(PERIOD, "2025-10-19 22:00", "2025-10-20 22:00")
    assert len(steps) == 24 and set(staged["Hydro_outage_cap"]) == {("CH00_psp_close", t, NAME) for t in steps}
    assert any("CH00_psp_close: 1 window, 24 of 144 hours in the period" in a for a in assumptions)


@pytest.mark.parametrize("rows, message", [
    ([("psp_close_Nant_de_Drance", "2025-10-21 00:00", "2025-10-22 00:00", 0, 0, "x")],
     "psp_close_Nant_de_Drance is not in the hydro fleet"),
    ([("CH03_nuclear", "2025-10-21 00:00", "2025-10-22 00:00", 0, 0, "x")], "CH03_nuclear is not in the hydro fleet"),
    ([("large_psp", "2025-10-21 00:00", "2025-10-23 00:00", 0, 0, "x"),
      ("large_psp", "2025-10-22 12:00", "2025-10-24 00:00", 0, 0, "y")], "windows of large_psp overlap"),
    ([("large_psp", "2025-10-21 00:00", "2025-10-22 00:00", 9000, 0, "x")], "above the installed turbine capacity"),
    ([("large_psp", "2025-10-21 00:00", "2025-10-22 00:00", 0, 3000, "x")], "above the installed pump capacity"),
    ([("large_psp", "2025-10-21 00:00", "2025-10-22 00:00", -1, 0, "x")], "negative"),
    ([("large_psp", "2025-13-01 00:00", "2025-10-22 00:00", 0, 0, "x")], "unparsable time"),
    ([("large_psp", "2025-10-21", "2025-10-22 00:00", 0, 0, "x")], "unparsable time"),
    ([("large_psp", "2025-10-26 02:30", "2025-10-27 00:00", 0, 0, "x")], "ambiguous or does not exist"),
    ([("large_psp", "2026-03-29 02:30", "2026-03-30 00:00", 0, 0, "x")], "ambiguous or does not exist"),
    ([("large_psp", "2025-10-22 00:00", "2025-10-21 00:00", 0, 0, "x")], "ends before it starts"),
    ([("large_psp", "2025-11-05 00:00", "2025-11-06 00:00", 0, 0, "x")], "lies entirely outside the period"),
])
def test_invalid_outages_raise(settings_for, tmp_path, rows, message):
    path = outage_file(tmp_path, rows)
    with pytest.raises(ValueError, match=message):
        read(settings_for(historical_hydro_outages=path))


UTC_COLUMNS = ["plant", "start_utc", "end_utc", "turbine_MW", "pump_MW", "source"]


def test_windows_in_utc_are_read_as_utc(settings_for, tmp_path):
    path = outage_file(tmp_path, [("large_psp", "2025-10-24 22:00", "2025-10-26 23:00", 4000, 1000, "a"),
                                  ("large_psp", "2025-10-26 23:00", "2025-10-27 01:00", 3000, 500, "b")],
                       columns=UTC_COLUMNS)
    staged, _, assumptions = read(settings_for(historical_hydro_outages=path))
    first = steps_between(PERIOD, "2025-10-24 22:00", "2025-10-26 23:00")
    second = steps_between(PERIOD, "2025-10-26 23:00", "2025-10-27 01:00")
    assert staged["Hydro_outage_cap"] == {**{("large_psp", t, NAME): (4000.0, 1000.0) for t in first},
                                          **{("large_psp", t, NAME): (3000.0, 500.0) for t in second}}
    assert any("large_psp: 2 windows, 51 of 51 hours in the period, turbines 3000.0-4000.0 MW, pumps 500.0-1000.0 MW "
               "(a; b)" in a for a in assumptions)


@pytest.mark.parametrize("columns", [
    ["plant", "start_local", "end_local", "start_utc", "end_utc", "turbine_MW", "pump_MW", "source"],
    ["plant", "start_local", "end_utc", "turbine_MW", "pump_MW", "source"],
])
def test_an_outage_table_needs_one_pair_of_time_columns(settings_for, tmp_path, columns):
    path = tmp_path / "outages.csv"
    pd.DataFrame([["large_psp"] + ["2025-10-21 00:00"] * (len(columns) - 4) + [0, 0, "x"]], columns=columns).to_csv(path, index=False)
    with pytest.raises(ValueError, match="needs start_local and end_local, or start_utc and end_utc"):
        read(settings_for(historical_hydro_outages=str(path)))


def test_overlapping_windows_are_found_whatever_their_order(settings_for, tmp_path):
    path = outage_file(tmp_path, [("large_psp", "2025-10-21 00:00", "2025-10-30 00:00", 0, 0, "long"),
                                  ("large_psp", "2025-10-22 00:00", "2025-10-23 00:00", 0, 0, "inside"),
                                  ("large_psp", "2025-10-25 00:00", "2025-10-26 00:00", 0, 0, "also inside")])
    with pytest.raises(ValueError, match="windows of large_psp overlap"):
        read(settings_for(historical_hydro_outages=path))


def test_outage_messages_replace_the_availability_factor(settings_for, tmp_path):
    path = outage_file(tmp_path, [("large_psp", "2025-10-21 00:00", "2025-10-22 00:00", 0, 0, "x")])
    settings = settings_for(historical_hydro_outages=path, historical_hydro_availability="outage_messages")
    fleet, assumptions = hi.read_hydro_fleet(NAME, settings)
    assert set(fleet["Avail_plant"].values()) == {1.0}
    assert any("availability 1.0" in a and "historical_hydro_outages" in a for a in assumptions)
    workbook = hi.read_hydro_fleet(NAME, settings_for(historical_hydro_outages=path))[0]
    assert set(workbook["Avail_plant"].values()) == {0.966}


def test_outage_messages_without_an_outage_file_or_an_unknown_mode_raise(settings_for):
    with pytest.raises(ValueError, match="'outage_messages' needs historical_hydro_outages"):
        hi.read_hydro_fleet(NAME, settings_for(historical_hydro_availability="outage_messages"))
    with pytest.raises(ValueError, match="historical_hydro_availability must be 'workbook' or 'outage_messages'"):
        hi.read_hydro_fleet(NAME, settings_for(historical_hydro_availability="ones"))


def test_an_outage_table_without_its_columns_or_file_raises(settings_for, tmp_path):
    path = outage_file(tmp_path, [("large_psp", "2025-10-21 00:00", "2025-10-22 00:00", 0, "x")],
                       columns=["plant", "start_local", "end_local", "turbine_MW", "source"])
    with pytest.raises(ValueError, match=r"lacks the columns \['pump_MW'\]"):
        read(settings_for(historical_hydro_outages=path))
    with pytest.raises(ValueError, match="no such file"):
        read(settings_for(historical_hydro_outages=str(tmp_path / "missing.csv")))
    with pytest.raises(ValueError, match="historical_hydro_outages must be False or the path of a CSV"):
        read(settings_for(historical_hydro_outages=True))


def test_the_outage_input_parses_for_the_wasta_fleet(settings_for):
    period = "2025-10-01/2026-07-31"
    settings = settings_for(period, historical_hydro_fleet="wasta_individual", historical_hydro_outages=hi.HYDRO_OUTAGES)
    caps = read(settings)[0]["Hydro_outage_cap"]
    by_plant = pd.Series(1, index=pd.MultiIndex.from_tuples(caps)).groupby(level=0).sum()
    oct_nov = len(steps_between(period, "2025-09-30 22:00", "2025-11-30 23:00"))
    from_february = len(steps_between(period, "2026-01-31 23:00", "2026-07-31 22:00"))
    assert by_plant.to_dict() == {"psp_close_Nant_de_Drance": oct_nov, "dam_Bieudron": from_february,
                                  "psp_close_Veytaux": len(hi.historical_time_map(period))}
    assert {caps[k] for k in caps if k[0] == "dam_Bieudron"} == {(846.0, 0.0)}


# --- model side -------------------------------------------------------------------------------------------------------

PLANTS = {"dam_a": ("dam", 100.0, 0.0), "open_b": ("psp_open", 50.0, 30.0), "loop_c": ("psp_close", 40.0, 40.0),
          "nuke": ("nuclear", 1000.0, 0.0)}
AVAIL = {"dam_a": 0.9, "open_b": 1.0, "loop_c": 1.0, "nuke": 1.0}
S = "s"


@pytest.fixture
def operation(monkeypatch):
    def stage(floor=None, reserve=None, caps=None):
        for p, (tech, _, _) in PLANTS.items():
            monkeypatch.setitem(ch.Map_plant_tech, p, tech)
        monkeypatch.setitem(ch.Hydro_operation, S, {"floor_MW": floor, "reserve_up_MW": reserve})
        for key, cap in (caps or {}).items():
            monkeypatch.setitem(ch.Hydro_outage_cap, key, cap)
    return stage


def operation_model():
    """Three storage-hydro plants and a nuclear plant over three steps; the closed loop is half available in t_2."""
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=list(PLANTS))
    m.P_gen = pyo.Set(initialize=list(PLANTS))
    m.P_pumping = pyo.Set(initialize=["open_b", "loop_c"])
    m.T = pyo.Set(initialize=["t_1", "t_2", "t_3"], ordered=True)
    m.Scenarios = pyo.Set(initialize=[S])
    m.avail_plant = pyo.Param(m.P, m.T, m.Scenarios, initialize={
        (p, t, S): 0.5 if (p, t) == ("loop_c", "t_2") else AVAIL[p] for p in PLANTS for t in ("t_1", "t_2", "t_3")})
    m.gen_max = pyo.Var(m.P_gen, m.Scenarios)
    m.pmp_max = pyo.Var(m.P_pumping, m.Scenarios)
    for p, (_, gen, pump) in PLANTS.items():
        m.gen_max[p, S].fix(gen)
        if p in ("open_b", "loop_c"):
            m.pmp_max[p, S].fix(pump)
    m.gen = pyo.Var(m.P_gen, m.T, m.Scenarios, initialize=0.0)
    m.storage_charge = pyo.Var(m.P_pumping, m.T, m.Scenarios, initialize=0.0)
    m.ConstraintNames = pyo.Set(initialize=["storage_soc"])  # the production scaling has no entry for the new rows
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=1e-3)
    return m


def linear(row):
    repn = generate_standard_repn(row.body, compute_values=True)
    return {var.name: coef for var, coef in zip(repn.linear_vars, repn.linear_coefs)}, repn.constant


def test_the_floor_sums_the_storage_hydro_plants_only(operation):
    operation(floor=120.0)
    m = operation_model()
    ch.add_hydro_operation(m)
    for t in ("t_1", "t_2", "t_3"):
        row = m.hydro_reserve_floor[t, S]
        coefficients, constant = linear(row)
        assert coefficients == {f"gen[{p},{t},{S}]": 1.0 for p in ("dam_a", "open_b", "loop_c")}  # nuke left out
        assert constant == 0 and pyo.value(row.lower) == 120.0 and row.upper is None
    assert ch.registered_constraints(m) == ["hydro_reserve_floor"]


def test_the_upward_reserve_caps_output_at_available_capacity_minus_the_reserve(operation):
    operation(reserve=30.0)
    m = operation_model()
    ch.add_hydro_operation(m)
    # available: dam 100 x 0.9 + open 50 + loop 40 (20 in t_2 at availability 0.5)
    assert [pyo.value(m.hydro_reserve_up_limit[t, S]) for t in m.T] == pytest.approx([150.0, 130.0, 150.0],
                                                                                       abs=TOLERANCE)
    coefficients, constant = linear(m.hydro_reserve_up["t_2", S])
    assert coefficients == {f"gen[{p},t_2,{S}]": 1.0 for p in ("dam_a", "open_b", "loop_c")} and constant == 0
    assert pyo.value(m.hydro_reserve_up["t_2", S].upper) == pytest.approx(130.0, abs=TOLERANCE)
    assert ch.registered_constraints(m) == ["hydro_reserve_up"]


def test_an_outage_lowers_the_available_capacity_and_caps_its_plant(operation):
    operation(reserve=30.0, caps={("dam_a", "t_2", S): (60.0, 0.0), ("loop_c", "t_3", S): (40.0, 10.0)})
    m = operation_model()
    ch.add_hydro_operation(m)
    assert pyo.value(m.hydro_reserve_up_limit["t_2", S]) == pytest.approx(60.0 + 50.0 + 20.0 - 30.0, abs=TOLERANCE)
    assert pyo.value(m.hydro_reserve_up_limit["t_3", S]) == pytest.approx(150.0, abs=TOLERANCE)  # cap above avail
    assert list(m.hydro_outage_turbine) == [("dam_a", "t_2", S), ("loop_c", "t_3", S)]
    assert pyo.value(m.hydro_outage_turbine["dam_a", "t_2", S].upper) == 60.0
    assert list(m.hydro_outage_pump) == [("loop_c", "t_3", S)]  # a dam has no pumping variable
    coefficients, _ = linear(m.hydro_outage_pump["loop_c", "t_3", S])
    assert coefficients == {f"storage_charge[loop_c,t_3,{S}]": 1.0}
    assert pyo.value(m.hydro_outage_pump["loop_c", "t_3", S].upper) == 10.0
    assert ch.registered_constraints(m) == ["hydro_reserve_up", "hydro_outage_turbine", "hydro_outage_pump"]


@pytest.mark.parametrize("floor, reserve, caps, message", [
    (131.0, None, {("dam_a", "t_2", S): (60.0, 0.0)}, r"floor of 131.0 MW exceeds the available storage-hydro turbine "
                                                       r"capacity of 130.0 MW in t_2 of s"),
    (101.0, 30.0, {("dam_a", "t_2", S): (60.0, 0.0)}, r"available capacity minus the upward reserve, 100.0 MW, is "
                                                       r"below the floor of 101.0 MW in t_2 of s"),
    (None, 161.0, None, r"available capacity minus the upward reserve, -1.0 MW, is below the floor of 0 MW in t_2"),
])
def test_floor_or_reserve_that_cannot_be_met_raise_before_solving(operation, floor, reserve, caps, message):
    operation(floor, reserve, caps)
    with pytest.raises(ValueError, match=message):
        ch.add_hydro_operation(operation_model())


def test_floor_and_reserve_that_fit_exactly_are_accepted(operation):
    operation(floor=100.0, reserve=30.0, caps={("dam_a", "t_2", S): (60.0, 0.0)})
    m = operation_model()
    ch.add_hydro_operation(m)
    assert pyo.value(m.hydro_reserve_up_limit["t_2", S]) == pytest.approx(100.0, abs=TOLERANCE)


@pytest.mark.parametrize("change, message", [
    (lambda mp: mp.delitem(ch.Hydro_operation, S), "no hydro operation is staged for s"),
    (lambda mp: mp.setitem(ch.Hydro_outage_cap, ("nuke", "t_1", S), (0.0, 0.0)), "outage of nuke, no storage-hydro"),
    (lambda mp: mp.setitem(ch.Hydro_outage_cap, ("dam_a", "t_9", S), (0.0, 0.0)), "outage step t_9 is no model step"),
])
def test_invalid_staging_raises(operation, monkeypatch, change, message):
    operation()
    change(monkeypatch)
    with pytest.raises(ValueError, match=message):
        ch.add_hydro_operation(operation_model())


def test_nothing_is_added_when_everything_is_off(operation):
    operation()
    m = operation_model()
    ch.add_hydro_operation(m)
    assert ch.registered_constraints(m) == []
    assert m.component("hydro_reserve_floor") is None and m.component("hydro_reserve_up") is None


def test_duals_of_the_new_constraints_are_exported_in_EUR_per_MWh(operation, tmp_path, monkeypatch):
    operation(floor=50.0, reserve=30.0, caps={("loop_c", "t_3", S): (40.0, 10.0)})
    m = operation_model()
    ch.add_hydro_operation(m)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    for i, t in enumerate(m.T):
        m.dual[m.hydro_reserve_floor[t, S]] = 10.0 + i
        m.dual[m.hydro_reserve_up[t, S]] = -20.0 - i
    m.dual[m.hydro_outage_turbine["loop_c", "t_3", S]] = -5.0
    m.dual[m.hydro_outage_pump["loop_c", "t_3", S]] = -7.0
    monkeypatch.chdir(tmp_path)
    res_export.constraints([m.hydro_reserve_floor, m.hydro_reserve_up, m.hydro_outage_turbine, m.hydro_outage_pump],
                           "run", m)
    folder = tmp_path / "output" / "run"
    floor = pd.read_csv(folder / "hydro_reserve_floor_dual.csv")
    assert list(floor.columns) == ["T", "Scenarios", "value"] and floor["value"].tolist() == [10.0, 11.0, 12.0]
    assert pd.read_csv(folder / "hydro_reserve_up_dual.csv")["value"].tolist() == [-20.0, -21.0, -22.0]
    turbine = pd.read_csv(folder / "hydro_outage_turbine_dual.csv")
    assert turbine.to_dict("list") == {"P_hydro_outage": ["loop_c"], "T": ["t_3"], "Scenarios": [S], "value": [-5.0]}
    assert pd.read_csv(folder / "hydro_outage_pump_dual.csv")["value"].tolist() == [-7.0]


def test_the_hook_adds_the_hydro_operation(operation, monkeypatch):
    operation(floor=50.0)
    monkeypatch.setattr(ch, "check_historical_model", lambda model, scenario_name, preset_file: None)
    monkeypatch.setattr(ch, "add_fixed_generation", lambda model: None)
    monkeypatch.setattr(ch, "add_reservoir_boundaries", lambda model: None)
    m = operation_model()
    ch.apply_historical_constraints(m, "hist", "unused.csv")
    assert ch.registered_constraints(m) == ["hydro_reserve_floor"]
