"""Unit tests for model/export_selection.py (setting params_not_exported)."""
import pyomo.environ as pyo
import pytest

from model.export_selection import indexed_params_to_export


def _model() -> pyo.ConcreteModel:
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=["a", "b"])
    m.avail_plant = pyo.Param(m.P, initialize=1.0)
    m.gen_max_limit = pyo.Param(m.P, initialize=2.0)
    m.weight = pyo.Param(initialize=1.0)
    return m


def test_named_params_are_left_out_and_scalars_are_never_exported():
    assert [p.name for p in indexed_params_to_export(_model(), ["avail_plant"])] == ["gen_max_limit"]


def test_nothing_named_exports_every_indexed_param():
    assert [p.name for p in indexed_params_to_export(_model(), [])] == ["avail_plant", "gen_max_limit"]


@pytest.mark.parametrize("name", ["avail_plants", "weight"])
def test_a_name_that_is_not_an_indexed_param_raises(name):
    with pytest.raises(ValueError, match=name):
        indexed_params_to_export(_model(), [name])
