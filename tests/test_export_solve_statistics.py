"""The solve-statistics export must import without the Gurobi Python package (gurobipy).

Pyomo calls Gurobi through its command-line launcher, so gurobipy is not a dependency (docs/running.md),
and CI does not install it.
"""
import importlib
import sys


def test_imports_without_gurobipy(monkeypatch):
    monkeypatch.setitem(sys.modules, "gurobipy", None)  # any `import gurobipy` now raises ImportError
    monkeypatch.delitem(sys.modules, "utils.export_solve_statistics", raising=False)
    importlib.import_module("utils.export_solve_statistics")
