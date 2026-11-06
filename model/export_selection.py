"""Which parameters model/core.py exports after the solve (setting params_not_exported)."""
import pyomo.environ as pyo


def indexed_params_to_export(model: pyo.ConcreteModel, not_exported: list[str]) -> list[pyo.Param]:
    """The model's active indexed Params without those named in not_exported.

    A name that is not an indexed Param of the model raises, so a typo cannot pass unnoticed.
    """
    params = [p for p in model.component_objects(ctype=pyo.Param, active=True, descend_into=True) if p.is_indexed()]
    unknown = sorted(set(not_exported) - {p.name for p in params})
    if unknown:
        raise ValueError(f"params_not_exported names {unknown}, which are not indexed parameters of the model")
    return [p for p in params if p.name not in set(not_exported)]
