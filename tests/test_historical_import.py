"""Unit tests for the historical import skeleton of a back-cast (data_prep/historical_import.py and its switch in
data_prep/data_import_TYNDP.py). They read the repository's tracked input files; no model is solved.
"""
import copy
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
import pytest

import data_prep.definitions_common as common
import data_prep.definitions_TYNDP as tyndp
import model.components_common as cc
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model.reset_items import get_lists_and_dicts
from model.structural_parameters import Map_tech_startcondition, Map_plant_tech_cost_component, fuel_limited_CH_list
from tests.backcast_data import GENERATION_TABLE, write_processed

HYDRO_FLEET = ["CH00_dam", "CH00_psp_close", "large_psp", "medium_reservior", "small_reservior"]
FLEET = HYDRO_FLEET + ["CH03_nuclear", "CH01_other"]
NAME = "hist_test_obs"


def containers():
    for module in (common, tyndp):
        lists, dicts = get_lists_and_dicts(module, [])
        for name in lists + dicts:
            yield f"{module.__name__}.{name}", getattr(module, name)


def snapshot():
    return {name: copy.deepcopy(container) for name, container in containers()}


@pytest.fixture
def isolated_globals():
    saved = [(container, copy.deepcopy(container)) for _, container in containers()]
    records = dict(hi.RUN_RECORDS)
    yield
    for container, content in saved:
        container.clear()
        container.update(content) if isinstance(container, dict) else container.extend(content)
    hi.RUN_RECORDS.clear()
    hi.RUN_RECORDS.update(records)


@pytest.fixture
def settings(monkeypatch, tmp_path):
    period = "2025-10-01/2025-10-02"
    monkeypatch.setitem(read_settings.scenarios_list, NAME, {
        "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": period,
        "historical_data_dir": str(write_processed(tmp_path / "processed", period))})
    return read_settings.read_scenario_settings(NAME)


def run_import():
    from data_prep.data_import_TYNDP import data_import_TYNDP_fcn

    data_import_TYNDP_fcn(NAME)


def test_staged_inputs_hold_the_fleet_the_lines_and_lost_load(settings):
    staged, files, assumptions = hi.read_historical_inputs(NAME, settings)
    assert staged["Plant_list"] == FLEET and staged["Map_node_plant"] == {"CH00": FLEET}
    # electrical basis of an open plant: discharge 1, charge the workbook round trip 0.87 x 0.87
    assert staged["Map_eff_out_plant"]["large_psp"] == 1.0 and staged["Map_eff_in_plant"]["large_psp"] == 0.87 * 0.87
    assert staged["Map_plant_startcondition"]["large_psp"] == Map_tech_startcondition["psp_open"]
    assert len(staged["Avail_plant"]) == len(FLEET) * len(settings["T_list"]) == 7 * 48
    assert staged["Avail_plant"][("large_psp", "t_6553", NAME)] == 0.966
    assert staged["cost_data_opr_slp"][("psp_open", NAME)] == 0.1
    for slopes in ("cost_data_inv_gen_slp", "cost_data_inv_e_slp", "cost_data_inv_discharge_slp"):
        assert set(staged[slopes].values()) == {0.0}
    assert staged["Map_line_node"]["HVAC_DE00_CH00"] == {"start_node": "DE00", "end_node": "CH00"}
    assert len(staged["Map_node_importinglineATC"]["CH00"]) == 4 and staged["Map_node_exportinglineATC"]["CH00"] == []
    assert 0 in staged["lost_load_cost"][NAME]
    assert hi.COST_WORKBOOK in files and any("representative 2030 fleet" in a for a in assumptions)


def test_import_stops_naming_exactly_the_inputs_not_built(settings, isolated_globals, monkeypatch):
    monkeypatch.setattr(hi, "NOT_YET_BUILT", ["an input", "another input"])  # every input is built today
    before = snapshot()
    with pytest.raises(NotImplementedError) as stop:
        run_import()
    assert all(name in str(stop.value) for name in hi.NOT_YET_BUILT)
    assert snapshot() == before


def test_failing_input_through_the_normal_entry_point_leaves_every_global_unchanged(settings, isolated_globals, monkeypatch):
    monkeypatch.setattr(hi, "NOT_YET_BUILT", [])

    def broken(techs):
        raise ValueError("broken availability table")

    monkeypatch.setattr(hi, "hydro_availability", broken)
    before = snapshot()
    with pytest.raises(ValueError, match="broken availability"):
        run_import()
    assert snapshot() == before and NAME not in common.lost_load_cost


def test_complete_inputs_are_written_and_recorded(settings, isolated_globals, monkeypatch, tmp_path):
    monkeypatch.setattr(hi, "NOT_YET_BUILT", [])
    run_import()
    assert common.Plant_list == FLEET and NAME in common.lost_load_cost
    assert len(common.Line_trade_price) == len(common.ATC_exportlimit) == len(common.ATC_importlimit) == 4 * 48
    assert len(common.Demand_data) == 48 and len(common.Infeed_consumers) == 5 * 48
    assert common.EV_inflexible_demand_data == {} and common.HP_inflexible_demand_data == {}
    assert sorted(common.Plant_inflow_list) == HYDRO_FLEET and len(common.Inflow_data) == 5 * 48
    assert len(common.Reservoir_start_stock) == 3 and common.Reservoir_end_target[NAME]["mode"] == "equal"
    hi.write_run_records(tmp_path, [NAME])
    time_map = pd.read_csv(tmp_path / "historical_time_map.csv")
    assert time_map["t"].tolist() == settings["T_list"]
    manifest = pd.read_json(tmp_path / "historical_inputs_manifest.json", typ="series")
    assert len(manifest["code"]["git_commit"]) == 40 and manifest["model_steps"] == [6553, 6600]
    assert all(len(record["sha256"]) == 64 for record in manifest["files"])
    read = [Path(record["path"]).name for record in manifest["files"]]
    tables = {hi.PRICES_TABLE, hi.NTC_TABLE, hi.DEMAND_TABLE, GENERATION_TABLE, hi.NUCLEAR_TABLE, hi.INSTALLED_TABLE,
              hi.PECD_TABLE, hi.RESERVOIR_TABLE}
    assert tables <= set(read) and len(read) == len(set(read))
    assert any(assumption.startswith("transfer limits") for assumption in manifest["assumptions"])


def test_run_without_backcast_writes_no_run_records(tmp_path):
    hi.write_run_records(tmp_path, ["not_a_backcast"])
    assert list(tmp_path.iterdir()) == []


def test_state_guard_raises_on_foreign_data_only(isolated_globals):
    hi.check_no_foreign_state(NAME)
    common.Plant_list.append("DE00_gas")
    with pytest.raises(RuntimeError, match="another meta-scenario"):
        hi.check_no_foreign_state(NAME)


def test_workbook_value_differing_between_years_raises(monkeypatch):
    import input.cost_operation_invest_data as operation_data

    monkeypatch.setitem(operation_data.cost_component["efficiency"], "psp_open", {2035: 0.87, 2050: 0.9})
    with pytest.raises(ValueError, match="differs"):
        hi.year_independent("efficiency", ["psp_open"])


def test_availability_varying_by_month_raises(monkeypatch, tmp_path):
    table = pd.read_csv(hi.AVAILABILITY_TABLE)
    table.loc[(table["country"] == "Default") & (table["technology"] == "psp_open"), "jan"] = 0.5
    path = tmp_path / "availability.csv"
    table.to_csv(path, index=False)
    monkeypatch.setattr(hi, "AVAILABILITY_TABLE", str(path))
    with pytest.raises(ValueError, match="varies by month"):
        hi.hydro_availability(["psp_open"])


def investment_parameters():
    """The production investment parameters on a small model of the fleet the globals hold."""
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=list(common.Plant_list))
    m.P_energymax = pyo.Set(initialize=[p for p in common.Plant_list
                                        if Map_plant_tech_cost_component[common.Map_plant_tech[p]] == "cap_op_energy"])
    m.Scenarios = pyo.Set(initialize=[NAME])
    m.PDH = pyo.Set(initialize=[])
    m.Fuels_limited = pyo.Set(initialize=cc.fuels_limited(True))
    return cc.define_params_inv(m, {NAME: 1}, False)


def test_zero_slopes_build_the_investment_parameters_and_each_one_is_needed(settings, isolated_globals, monkeypatch):
    monkeypatch.setattr(hi, "NOT_YET_BUILT", [])
    run_import()
    m = investment_parameters()
    assert {pyo.value(m.investment_genmax_slp[p, NAME]) for p in m.P} == {0.0}
    del common.cost_data_inv_gen_slp[("psp_open", NAME)]
    with pytest.raises(KeyError):
        investment_parameters()


def test_backcast_has_no_limited_fuels():
    assert cc.fuels_limited(True) == []
    assert cc.fuels_limited(False) == list(fuel_limited_CH_list)
