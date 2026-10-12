"""Unit tests for the dual export in aggregation/results_export.py.

Rows are scaled by sf on both sides in the model, so the solver's dual is 1/sf times the dual of
the original constraint. These tests set solver duals by hand (no solver) and check that the
exported values are in the units of the unscaled constraint, and which rows are exported.
"""
import pandas as pd
import pyomo.environ as pyo
import pytest

from aggregation.results_export import MissingDualsError, constraints

SCALING = {"generation_limit": 0.1, "building_heat_demand": 100.0, "consume_tot_limit": 1e-3}


def _model():
    m = pyo.ConcreteModel()
    m.ConstraintNames = pyo.Set(initialize=list(SCALING))
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=SCALING, mutable=True)
    m.T = pyo.Set(initialize=["t_1", "t_2"])
    m.x = pyo.Var(m.T)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    return m


def test_indexed_dual_is_multiplied_by_its_scaling_factor():
    m = _model()
    m.generation_limit = pyo.Constraint(m.T, rule=lambda m, t: 0.1 * m.x[t] <= 0.1 * 5)
    m.dual[m.generation_limit["t_1"]] = -854.28
    m.dual[m.generation_limit["t_2"]] = 0.0
    out = constraints([m.generation_limit], "unused", m, write_csv=False)["generation_limit"]
    assert out["value"].tolist() == pytest.approx([-85.428, 0.0])


def test_factor_above_one_scales_up():
    m = _model()
    m.building_heat_demand = pyo.Constraint(m.T, rule=lambda m, t: 100 * m.x[t] == 100 * 1)
    m.dual[m.building_heat_demand["t_1"]] = 0.5
    m.dual[m.building_heat_demand["t_2"]] = -0.25
    out = constraints([m.building_heat_demand], "unused", m, write_csv=False)["building_heat_demand"]
    assert out["value"].tolist() == pytest.approx([50.0, -25.0])


def test_per_plant_consume_tot_limit_uses_shared_factor():
    m = _model()
    name = "consume_tot_limit_CH00_electrolyzer_1_2050_st_wy1995_aa"
    m.add_component(name, pyo.Constraint(expr=1e-3 * m.x["t_1"] <= 1e-3 * 10))
    con = m.component(name)
    m.dual[con] = 2000.0
    out = constraints([con], "unused", m, write_csv=False)[name]
    assert out["value"].tolist() == pytest.approx([2.0])


def test_constraint_without_scaling_entry_is_exported_unchanged():
    # a constraint missing from the scaling map is not scaled in the model, so its factor is 1
    m = _model()
    m.unlisted_limit = pyo.Constraint(m.T, rule=lambda m, t: m.x[t] <= 1)
    m.dual[m.unlisted_limit["t_1"]] = -3.5
    m.dual[m.unlisted_limit["t_2"]] = 0.0
    out = constraints([m.unlisted_limit], "unused", m, write_csv=False)["unlisted_limit"]
    assert out["value"].tolist() == pytest.approx([-3.5, 0.0])


def _limits(m):
    """generation_limit over T x {a, b}, scaled by 0.1, with a solver dual for every row."""
    m.P = pyo.Set(initialize=["a", "b"])
    m.y = pyo.Var(m.P, m.T)
    m.generation_limit = pyo.Constraint(m.P, m.T, rule=lambda m, p, t: 0.1 * m.y[p, t] <= 0.1 * 5)
    for k, row in enumerate(m.generation_limit.values()):
        m.dual[row] = -10.0 * (k + 1)
    return m.generation_limit


def test_deactivated_row_is_left_out_and_the_other_rows_are_exported():
    m = _model()
    limits = _limits(m)
    limits["b", "t_1"].deactivate()
    del m.dual[limits["b", "t_1"]]  # the solver returns no dual for a deactivated row
    out = constraints([limits], "unused", m, write_csv=False)["generation_limit"]
    assert out.columns.tolist() == ["P", "T", "value"]
    assert out.values.tolist() == [["a", "t_1", pytest.approx(-1.0)], ["a", "t_2", pytest.approx(-2.0)],
                                   ["b", "t_2", pytest.approx(-4.0)]]


def test_active_row_without_dual_fails_after_the_other_constraints_are_written(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    m = _model()
    limits = _limits(m)
    del m.dual[limits["a", "t_2"]]
    m.building_heat_demand = pyo.Constraint(m.T, rule=lambda m, t: 100 * m.x[t] == 100 * 1)
    for row in m.building_heat_demand.values():
        m.dual[row] = 0.5
    with pytest.raises(MissingDualsError, match=r"no dual for 1 active rows.*generation_limit\[\('a', 't_2'\)\]") as error:
        constraints([limits, m.building_heat_demand], "run", m, write_csv=True)
    assert error.value.missing == ["generation_limit[('a', 't_2')]"]
    assert list(error.value.exported) == ["building_heat_demand"]
    written = sorted(path.name for path in (tmp_path / "output" / "run").iterdir())
    assert written == ["building_heat_demand_dual.csv"]
    assert pd.read_csv(tmp_path / "output" / "run" / "building_heat_demand_dual.csv")["value"].tolist() == [50.0, 50.0]


@pytest.mark.parametrize("make_row", [
    lambda m: pyo.Constraint(expr=m.x["t_1"] <= m.capacity),  # no finite bound, e.g. the last lost-load step
    lambda m: pyo.Constraint(expr=2 * m.x["t_2"] <= 3),  # x[t_2] fixed below: no free variable
])
def test_row_never_sent_to_the_solver_is_left_out_with_a_note(capsys, make_row):
    m = _model()
    m.capacity = pyo.Param(mutable=True, initialize=float("inf"))  # a bound that evaluates to infinity
    m.x["t_2"].fix(1.0)
    m.add_component("unlisted_bound", make_row(m))
    limits = _limits(m)
    out = constraints([m.unlisted_bound, limits], "unused", m, write_csv=False)
    assert list(out) == ["generation_limit"] and len(out["generation_limit"]) == 4
    assert "unlisted_bound: 1 rows without a finite bound or a free variable have no dual" in capsys.readouterr().out


def test_empty_constraint_writes_nothing():
    m = _model()
    m.Empty = pyo.Set(initialize=[])
    m.empty_limit = pyo.Constraint(m.Empty, rule=lambda m, e: m.x["t_1"] <= 1)
    assert constraints([m.empty_limit], "unused", m, write_csv=False) == {}
