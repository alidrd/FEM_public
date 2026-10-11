"""Unit tests for the back-cast hook and its checks (model/components_historical.py) and the default dual export
list (model/core.py). The tests build small Pyomo models (no solver).
"""
import pyomo.environ as pyo
import pytest

from model import components_historical as ch

FLEET = {"large_psp": "psp_open", "CH00_psp_close": "psp_close", "CH00_dam": "dam"}
META = "hist_test"


@pytest.fixture
def presets(tmp_path):
    """A preset file whose only row belongs to another meta-scenario."""
    path = tmp_path / "presets.csv"
    path.write_text("# variable_name,scenario_name,index1,index2,value\ngen_max,other_meta,large_psp,other_meta_s,1\n")
    return path


@pytest.fixture
def model(monkeypatch):
    """A small model shaped like a back-cast: the hydro fleet with fixed capacities and hydro O&M."""
    for plant, tech in FLEET.items():
        monkeypatch.setitem(ch.Map_plant_tech, plant, tech)
        monkeypatch.setitem(ch.Map_plant_node, plant, "CH00")
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=list(FLEET))
    m.PDH = pyo.Set(initialize=[])
    m.P_dsr = pyo.Set(initialize=[])
    m.T = pyo.Set(initialize=["t_1", "t_2"])
    m.Scenarios = pyo.Set(initialize=["s"])
    m.gen_max = pyo.Var(m.P, m.Scenarios)
    for index in m.gen_max:
        m.gen_max[index].fix(100.0)
    m.gen = pyo.Var(m.P, m.T, m.Scenarios)
    m.cost_inv_dict = {(p, "s"): 0.0 * m.gen_max[p, "s"] for p in m.P}  # investment is off: zero slopes
    m.cost_op_dict = {(p, "s"): sum(0.1 * m.gen[p, t, "s"] for t in m.T) for p in m.P}
    return m


def test_conforming_model_passes(model, presets):
    ch.check_historical_model(model, META, presets)
    lines = ch.audit_objective(model)
    assert "cost_op_dict (EUR): 3 active terms, constant offset 0.0" in lines
    assert "cost_inv_dict (CHF): 0 active terms, constant offset 0.0" in lines


def add_plant(model, monkeypatch, plant, tech, node):
    monkeypatch.setitem(ch.Map_plant_tech, plant, tech)
    monkeypatch.setitem(ch.Map_plant_node, plant, node)
    model.P.add(plant)


@pytest.mark.parametrize("violation, message", [
    (lambda m, mp: add_plant(m, mp, "CH00_gas", "gas", "CH00"), "outside the back-cast fleet"),
    (lambda m, mp: add_plant(m, mp, "DE00_psp_open", "psp_open", "DE00"), "outside the back-cast fleet"),
    (lambda m, mp: m.PDH.add("DH_medium_hp"), "district-heating plants"),
    (lambda m, mp: m.gen_max["large_psp", "s"].unfix(), "investment variable gen_max is free"),
    (lambda m, mp: setattr(m, "energy_limit", pyo.Constraint(expr=m.gen["large_psp", "t_1", "s"] <= 5)), "energy_limit"),
    (lambda m, mp: m.add_component("consume_tot_limit_CH00_electrolyzer_1_s",
                                   pyo.Constraint(expr=m.gen["large_psp", "t_1", "s"] <= 5)), "consume_tot_limit"),
    (lambda m, mp: setattr(m, "ev_consumption_weekly_sum", pyo.Constraint(expr=m.gen["large_psp", "t_1", "s"] == 1)),
     "ev_consumption_weekly_sum"),
    (lambda m, mp: setattr(m, "Constraint_winter_limit", pyo.Constraint(expr=m.gen["large_psp", "t_1", "s"] <= 1)),
     "Constraint_winter_limit"),
    (lambda m, mp: m.P_dsr.add("CH00_dsr"), "demand-side response"),
    (lambda m, mp: setattr(m, "slack_soc", True), "slack_soc"),
])
def test_each_violation_raises_naming_it(model, presets, monkeypatch, violation, message):
    violation(model, monkeypatch)
    with pytest.raises(ValueError, match=message):
        ch.check_historical_model(model, META, presets)


def test_inactive_constraint_is_no_violation(model, presets):
    model.energy_limit = pyo.Constraint(expr=model.gen["large_psp", "t_1", "s"] <= 5)
    model.energy_limit.deactivate()
    ch.check_historical_model(model, META, presets)


@pytest.mark.parametrize("scenario_column", [META, ""])
def test_preset_for_the_backcast_raises(model, tmp_path, scenario_column):
    path = tmp_path / "presets.csv"
    path.write_text(f"gen_max,{scenario_column},large_psp,{META}_s,1\n")
    with pytest.raises(ValueError, match="variable presets apply"):
        ch.check_historical_model(model, META, path)


def test_investment_cost_raises_and_a_fixed_cost_is_a_constant_offset(model):
    model.gen["large_psp", "t_1", "s"].fix(10.0)
    assert "cost_op_dict (EUR): 3 active terms, constant offset 1.0" in ch.audit_objective(model)
    model.cost_inv_dict[("large_psp", "s")] = 5.0 * model.gen_max["large_psp", "s"]
    with pytest.raises(ValueError, match="investment cost"):
        ch.audit_objective(model)


def test_constraint_added_through_the_hook_is_in_the_default_dual_export(model):
    from model.core import dual_constraint_names

    default = dual_constraint_names(pyo.ConcreteModel(), False, False)
    assert ch.registered_constraints(model) == []
    ch.add_backcast_constraint(model, "reservoir_end_target", pyo.Constraint(expr=model.gen["large_psp", "t_1", "s"] >= 0))
    assert dual_constraint_names(model, False, False) == default + ["reservoir_end_target"]
    assert dual_constraint_names(model, True, False)[-2:] == ["Constraint_winter_limit", "reservoir_end_target"]
