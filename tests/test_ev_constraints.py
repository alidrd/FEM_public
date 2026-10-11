"""Unit tests for the flexible-EV constraints in model/components_common.py.

The weekly EV energy constraint and the hourly EV charging constraint name the plant CH00_EV_flex. They
are built only when that plant belongs to the model; a back-cast builds no EV plant. The tests build a
small Pyomo model (no solver) whose week has three hours, two of them modelled.
"""
import pyomo.environ as pyo
import pytest
from pyomo.repn import generate_standard_repn

import model.components_common as cc

EV = "CH00_EV_flex"
WEEK = ["t_1", "t_2", "t_3"]
MODELLED = WEEK[:2]
SCALE = 0.5


def _model(monkeypatch, plants):
    monkeypatch.setitem(cc.EV_weekly_energy_consumption_data, ("week_1", "s"), 30.0)
    for t, rate in zip(MODELLED, [8.0, 9.0]):
        monkeypatch.setitem(cc.EV_charging_power_rate, (t, "s"), rate)
    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=MODELLED, ordered=True)
    m.Scenarios = pyo.Set(initialize=["s"])
    m.Week = pyo.Set(initialize=["week_1"], ordered=True)
    m.Map_week_t = pyo.Param(m.Week, initialize={"week_1": MODELLED}, within=pyo.Any)
    m.Map_week_t_full = pyo.Param(m.Week, initialize={"week_1": WEEK}, within=pyo.Any)
    m.P = pyo.Set(initialize=plants)
    m.P_pumping = pyo.Set(initialize=plants, within=m.P)
    m.storage_charge = pyo.Var(m.P_pumping, m.T, m.Scenarios, domain=pyo.NonNegativeReals)
    m.ConstraintNames = pyo.Set(initialize=["ev_consumption_weekly_sum", "ev_consumption_hourly_rate"])
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=SCALE)
    cc.define_ev_constraints(m)
    return m


def _coefficients(expression):
    repn = generate_standard_repn(expression)
    return {var.name: coef for var, coef in zip(repn.linear_vars, repn.linear_coefs)}


def test_model_without_ev_plant_builds_no_ev_constraint(monkeypatch):
    m = _model(monkeypatch, ["CH00_battery"])
    assert m.component("ev_consumption_weekly_sum") is None
    assert m.component("ev_consumption_hourly_rate") is None


def test_model_with_ev_plant_keeps_both_constraints(monkeypatch):
    m = _model(monkeypatch, [EV, "CH00_battery"])
    weekly = m.ev_consumption_weekly_sum["week_1", "s"]
    # two of three hours are modelled, so the weekly 30 MWh is scaled to 20 MWh
    assert _coefficients(weekly.body) == {
        m.storage_charge[EV, t, "s"].name: pytest.approx(SCALE) for t in MODELLED
    }
    assert pyo.value(weekly.lower) == pyo.value(weekly.upper) == pytest.approx(SCALE * 20.0)
    for t, rate in zip(MODELLED, [8.0, 9.0]):
        hourly = m.ev_consumption_hourly_rate[t, "s"]
        assert _coefficients(hourly.body) == {m.storage_charge[EV, t, "s"].name: pytest.approx(SCALE)}
        assert pyo.value(hourly.upper) == pytest.approx(SCALE * rate)
