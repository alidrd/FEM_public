"""Build a back-cast from a scenario CSV in this fresh process, without solving, and save its canonical model data.

Usage: python -m tests.backcast_build <scenario csv> <meta-scenario> <output pickle> [<run_year> <weather_year>]

Run by the needs_data tests in tests/test_backcast_scenario.py, one process per build: the data import fills
module-level lists that nothing clears. The scenarios come from the production loader (scenarios/scenarios.py)
with its target_csv line pointing at the given CSV, and the model from core_main(..., solve=False). A spy fails the
build if any solver is created. The build writes into output/pytest_<meta-scenario>, which is removed afterwards.
"""
import hashlib
import pickle
import re
import shutil
import sys
import types
from pathlib import Path

import pyomo.environ as pyo
import pyomo.opt
from pyomo.repn import generate_standard_repn


def no_solver(*args, **kwargs):
    raise AssertionError("building the back-cast created a solver")


def load_scenarios(csv_path):
    """Import scenarios/scenarios.py as the module scenarios.scenarios, reading csv_path instead of target_csv."""
    import scenarios

    source = Path("scenarios/scenarios.py").read_text(encoding="utf-8")
    source, count = re.subn(r'^target_csv = "[^"]*"', f'target_csv = "{csv_path}"', source, flags=re.M)
    if count != 1:
        raise RuntimeError(f"scenarios/scenarios.py has {count} target_csv lines, expected 1")
    module = types.ModuleType("scenarios.scenarios")
    module.__file__ = str(Path("scenarios/scenarios.py").resolve())
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    sys.modules["scenarios.scenarios"] = scenarios.scenarios = module
    return module


def linear(expression):
    repn = generate_standard_repn(expression, compute_values=True)
    if repn.nonlinear_expr is not None or repn.quadratic_vars:
        raise ValueError(f"not linear: {expression}")
    return tuple(sorted((var.name, coef) for var, coef in zip(repn.linear_vars, repn.linear_coefs))), repn.constant


def fingerprint(value):
    """Exact fingerprint of canonical data: repr keeps every float exactly."""
    return hashlib.sha256(repr(value).encode()).hexdigest()[:16]


def canonical(model):
    """Fingerprints of the coefficients, bounds and right-hand sides of each active constraint row and of the
    bounds of each variable, by name, and of the objective coefficients; LP text and order play no role."""
    rows = {}
    for row in model.component_data_objects(pyo.Constraint, active=True):
        coefficients, constant = linear(row.body)
        rows[row.name] = fingerprint((pyo.value(row.lower) if row.lower is not None else None,
                                      pyo.value(row.upper) if row.upper is not None else None, coefficients,
                                      constant))
    variables = {var.name: fingerprint((var.lb, var.ub, var.fixed, var.value if var.fixed else None,
                                        var.domain.name)) for var in model.component_data_objects(pyo.Var)}
    return {"constraints": rows, "variables": variables, "objective": fingerprint(linear(model.OBJ.expr))}


def main(csv_path, meta, output, run_year=None, weather_year=None):
    pyo.SolverFactory = pyomo.opt.SolverFactory = no_solver
    module = load_scenarios(csv_path)
    import model.read_settings as read_settings

    sub_scenarios = module.meta_scenarios_list[meta]
    for sub in sub_scenarios:
        read_settings.scenarios_list[sub] = dict(module.scenarios_list[sub])
        if run_year is not None:
            read_settings.scenarios_list[sub].update(run_year=int(run_year), weather_year=int(weather_year))
    from model import core

    try:
        model = core.core_main(f"pytest_{meta}", sub_scenarios, solve=False)
    finally:
        shutil.rmtree(Path("output") / f"pytest_{meta}", ignore_errors=True)
    summary = {"plants": sorted(model.P), "lines": sorted(model.lineATC), "consumers": sorted(model.Consumer),
               "district_heating_plants": sorted(model.PDH), "steps": len(model.T),
               "objective_active": model.OBJ.active}
    with open(output, "wb") as handle:
        pickle.dump({"summary": summary, "canonical": canonical(model)}, handle)


if __name__ == "__main__":
    main(*sys.argv[1:])
