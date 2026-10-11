"""Unit tests for the CH_only trade price in model/components_common.py.

Day-ahead prices are often negative. The trade price parameter must keep a negative price, and the
objective must price that hour's imports at it. The tests build a small Pyomo model (no solver).
"""
import pyomo.environ as pyo
import pytest
from pyomo.repn import generate_standard_repn

import model.components_common as cc

LINE = "HVAC_DE00_CH00"  # neighbour as start node, CH00 as end node: positive flow imports into CH


def _model(monkeypatch, prices, steps=None, lines=(LINE,)):
    for t, price in prices.items():
        monkeypatch.setitem(cc.Line_trade_price, (LINE, t, "s"), price)
    m = pyo.ConcreteModel()
    m.lineATC = pyo.Set(initialize=list(lines))
    m.T = pyo.Set(initialize=steps or list(prices), ordered=True)
    m.Scenarios = pyo.Set(initialize=["s"])
    cc.define_line_trade_price(m)
    return m


def _price_line_at_ch00(monkeypatch):
    monkeypatch.setitem(cc.Map_node_importinglineATC, "CH00", [LINE])
    monkeypatch.setitem(cc.Map_node_exportinglineATC, "CH00", [])


def test_negative_price_is_kept(monkeypatch):
    m = _model(monkeypatch, {"t_1": 40.0, "t_2": -25.5})
    assert pyo.value(m.line_trade_price[LINE, "t_2", "s"]) == -25.5


def test_objective_prices_imports_at_negative_price(monkeypatch):
    m = _model(monkeypatch, {"t_1": 40.0, "t_2": -25.5})
    monkeypatch.setitem(cc.Map_node_importinglineATC, "CH00", [LINE])
    monkeypatch.setitem(cc.Map_node_exportinglineATC, "CH00", [])
    # obj_expression loops over these sets; empty, they add no cost term
    for name in ["P", "Consumer", "lostLoad_step", "P_fuellimCH", "P_pumping", "PDH_allinvTh",
                 "PDH_costExclusivelyOnThermalSide", "Fuels_limited", "NodeDH"]:
        m.add_component(name, pyo.Set(initialize=[]))
    m.weight_in_objective_fcn = pyo.Param(m.Scenarios, initialize={"s": 1.0})
    m.Export = pyo.Var(m.lineATC, m.T, m.Scenarios)
    repn = generate_standard_repn(cc.obj_expression(m))
    coefficient = {var.name: coef for var, coef in zip(repn.linear_vars, repn.linear_coefs)}
    assert coefficient[m.Export[LINE, "t_2", "s"].name] == pytest.approx(-25.5)
    assert coefficient[m.Export[LINE, "t_1", "s"].name] == pytest.approx(40.0)


def test_missing_price_on_a_priced_line_raises(monkeypatch):
    # sub-scenarios with different time windows can leave a step without a price; it must not become 0
    _price_line_at_ch00(monkeypatch)
    with pytest.raises(ValueError, match="no trade price"):
        _model(monkeypatch, {"t_1": 40.0}, steps=["t_1", "t_2"])


def test_line_away_from_ch00_carries_zero(monkeypatch):
    _price_line_at_ch00(monkeypatch)
    m = _model(monkeypatch, {"t_1": 40.0}, lines=(LINE, "HVAC_DE00_FR00"))
    assert pyo.value(m.line_trade_price["HVAC_DE00_FR00", "t_1", "s"]) == 0


def test_run_without_trade_prices_builds_zeros(monkeypatch):
    # runs that are not CH_only have no trade prices, and the objective adds no trade cost
    m = _model(monkeypatch, {}, steps=["t_1"])
    assert pyo.value(m.line_trade_price[LINE, "t_1", "s"]) == 0
