"""Tests of the first back-cast scenario, scenarios/scen_to_run_CH_only_historical.csv.

The needs_data tests read the processed folder the scenario names. Each build runs tests/backcast_build.py in a
fresh process, through core_main(..., solve=False) with a spy on every solver. The other tests check that helper on
small inputs and run everywhere.
"""
import ast
import pickle
import subprocess
import sys

import pandas as pd
import pyomo.environ as pyo
import pytest

from tests import backcast_build
from tests.local_data import REPO_ROOT, require_local_data

SCENARIO_CSV = "scenarios/scen_to_run_CH_only_historical.csv"
META = "hist_2526_octaug"
# The scenario keeps the default run_year 2050 and weather_year 1995; the variants cover 2035, 2008 and 2009
VARIANTS = [(2035, 1995), (2050, 2008), (2050, 2009)]
FLEET = ["CH00_dam", "CH00_psp_close", "CH01_other", "CH03_nuclear", "large_psp", "medium_reservior",
         "small_reservior"]
BORDER_LINES = ["HVAC_AT00_CH00", "HVAC_DE00_CH00", "HVAC_FR00_CH00", "HVAC_IT00_CH00"]


def require_processed_tables():
    """Fail, naming each one, if a table the back-cast reads is missing from the scenario's processed folder."""
    from data_prep import historical_import as hi
    from tests.backcast_data import GENERATION_TABLE

    folder = ast.literal_eval(pd.read_csv(REPO_ROOT / SCENARIO_CSV, index_col=0).loc["historical_data_dir", META])
    tables = [hi.PRICES_TABLE, hi.NTC_TABLE, hi.DEMAND_TABLE, GENERATION_TABLE, hi.NUCLEAR_TABLE, hi.INSTALLED_TABLE,
              hi.PECD_TABLE, hi.RESERVOIR_TABLE]
    require_local_data(*[f"{folder}/{table}" for table in tables])


def build(folder, run_year=None, weather_year=None):
    output = folder / f"build_{run_year}_{weather_year}.pkl"
    years = [] if run_year is None else [str(run_year), str(weather_year)]
    result = subprocess.run([sys.executable, "-m", "tests.backcast_build", SCENARIO_CSV, META, str(output), *years],
                            cwd=REPO_ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-3000:]
    with open(output, "rb") as handle:
        return pickle.load(handle)


@pytest.fixture(scope="module")
def as_is(tmp_path_factory):
    """The back-cast as the scenario file defines it."""
    require_processed_tables()
    return build(tmp_path_factory.mktemp("backcast"))


@pytest.mark.needs_data
def test_the_historical_import_alone_builds_the_backcast_model(as_is):
    assert as_is["summary"] == {"plants": FLEET, "lines": BORDER_LINES, "consumers": ["CH00_fixedconsumer"],
                                "district_heating_plants": [], "steps": 8016, "objective_active": True}


def differences(reference, variant):
    found = []
    for part in ("constraints", "variables"):
        ref, var = reference[part], variant[part]
        found += [f"{part}: {name} only in the reference" for name in sorted(set(ref) - set(var))]
        found += [f"{part}: {name} only in the variant" for name in sorted(set(var) - set(ref))]
        found += [f"{part}: {name} differs" for name in sorted(set(ref) & set(var)) if ref[name] != var[name]]
    if reference["objective"] != variant["objective"]:
        found.append("objective coefficients differ")
    return found


@pytest.mark.needs_data
@pytest.mark.parametrize("run_year, weather_year", VARIANTS)
def test_run_year_and_weather_year_leave_the_model_unchanged(as_is, tmp_path, run_year, weather_year):
    found = differences(as_is["canonical"], build(tmp_path, run_year, weather_year)["canonical"])
    assert not found, f"{len(found)} differences, e.g. {found[:50]}"


# --- the helper, on small inputs --------------------------------------------------------------------------------

def small_model(coefficient):
    m = pyo.ConcreteModel()
    m.T = pyo.Set(initialize=["t_1", "t_2"])
    m.x = pyo.Var(m.T, bounds=(0, 10))
    m.limit = pyo.Constraint(m.T, rule=lambda m, t: (coefficient if t == "t_2" else 1.0) * m.x[t] <= 5)
    m.OBJ = pyo.Objective(expr=sum(m.x[t] for t in m.T))
    return m


def test_canonical_data_differ_exactly_where_a_coefficient_differs():
    reference = backcast_build.canonical(small_model(2.0))
    same = differences(reference, backcast_build.canonical(small_model(2.0)))
    changed = differences(reference, backcast_build.canonical(small_model(2.0 + 1e-12)))
    assert same == [] and changed == ["constraints: limit[t_2] differs"]


def test_a_variable_bound_or_objective_difference_is_listed():
    reference, variant = small_model(2.0), small_model(2.0)
    variant.x["t_1"].setub(11)
    variant.OBJ.expr = 2 * variant.x["t_1"] + variant.x["t_2"]
    assert differences(backcast_build.canonical(reference), backcast_build.canonical(variant)) == [
        "variables: x[t_1] differs", "objective coefficients differ"]


def test_scenarios_load_from_the_given_csv_with_the_production_loader(tmp_path, monkeypatch):
    import scenarios

    monkeypatch.setitem(sys.modules, "scenarios.scenarios", sys.modules.get("scenarios.scenarios"))
    monkeypatch.setattr(scenarios, "scenarios", getattr(scenarios, "scenarios", None), raising=False)
    csv = tmp_path / "scen.csv"
    csv.write_text("Item,bc\nsub_secn,obs\nweight_in_objective_fcn,1\nhistorical_period,'2025-10-01/2025-10-07'\n")
    module = backcast_build.load_scenarios(csv.as_posix())
    assert module.meta_scenarios_list == {"bc": ["bc_obs"]}
    assert module.scenarios_list["bc_obs"]["historical_period"] == "2025-10-01/2025-10-07"
    assert sys.modules["scenarios.scenarios"] is module
