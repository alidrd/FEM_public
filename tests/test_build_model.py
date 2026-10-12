"""Unit tests for building the model without solving it: core_main(..., solve=False) in model/core.py. Each build
runs in a fresh process, because the data import fills module-level lists that nothing clears; it builds a 25-hour
CH_only run from the tracked inputs.
"""
import subprocess
import sys
import textwrap

BUILD = textwrap.dedent("""
    import shutil
    import sys

    import pyomo.environ
    import pyomo.opt

    class Solved(Exception):
        pass

    class FakeSolver:
        options = {}

        def solve(self, model, **kwargs):
            raise Solved(model)

    def solver_factory(*args, **kwargs):
        if sys.argv[1] == "build":
            raise AssertionError("a solver was created")
        return FakeSolver()

    pyomo.environ.SolverFactory = pyomo.opt.SolverFactory = solver_factory  # a spy on every solver

    import model.read_settings as read_settings
    read_settings.scenarios_list["pytest_build_obs"] = {
        "weight_in_objective_fcn": 1, "CH_only": True, "t_start": 6553, "t_end": 6577, "weather_year": 1995,
        "run_year": 2050, "neighbor_price_scenario_for_CH_only_mode": "FEMbase_zeroPV_avg",
        "lost_load_cost_mode": "multi_step_v1"}
    from model import core
    try:
        if sys.argv[1] == "build":
            model = core.core_main("pytest_build", ["pytest_build_obs"], solve=False)
        else:
            try:
                core.core_main("pytest_build", ["pytest_build_obs"])
            except Solved as solved:
                model = solved.args[0]
    finally:
        shutil.rmtree("output/pytest_build", ignore_errors=True)
    print("built", len(model.T), sorted(model.Node), model.OBJ.active, len(model.energy_balance))
""")


def build(mode):
    result = subprocess.run([sys.executable, "-c", BUILD, mode], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


def test_solve_false_returns_the_built_model_and_creates_no_solver():
    assert build("build") == "built 25 ['CH00'] True 25"


def test_a_normal_call_solves_the_model_it_builds():
    assert build("solve") == "built 25 ['CH00'] True 25"


def test_model_core_imports_without_gurobipy():
    blocked = "import sys; sys.modules['gurobipy'] = None; import model.core"  # None makes `import gurobipy` fail
    result = subprocess.run([sys.executable, "-c", blocked], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-2000:]
