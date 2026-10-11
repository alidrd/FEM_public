"""Run every meta-scenario of the scenario CSV named on the target_csv line of scenarios/scenarios.py.

Each meta-scenario runs in its own Python process through run_scenarios_hpc.py. The data import fills
module-level lists and dicts that nothing clears, so one process per meta-scenario keeps the data of one
meta-scenario out of the next. A failing meta-scenario stops the batch.
"""
import subprocess
import sys
from pathlib import Path

from scenarios.scenarios import meta_scenarios_list

SINGLE_SCENARIO_RUNNER = Path(__file__).resolve().parent / "run_scenarios_hpc.py"


def run_all(scenario_names):
    for scenario_name in scenario_names:
        exit_code = subprocess.run([sys.executable, str(SINGLE_SCENARIO_RUNNER), "--scenario", scenario_name]).returncode
        if exit_code != 0:
            sys.exit(f"Meta-scenario {scenario_name} failed with exit code {exit_code}; the remaining meta-scenarios were not run.")


if __name__ == "__main__":
    run_all(list(meta_scenarios_list))
