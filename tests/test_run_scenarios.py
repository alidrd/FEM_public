"""Unit tests for run_scenarios.py: each meta-scenario runs in its own Python process, and a failure stops
the batch.

The data import fills module-level lists and dicts that nothing clears, so a meta-scenario run in the same
process as another would inherit its plants and lines. The tests replace the single-scenario runner with a
small script that records its process id (no model, no solver).
"""
import importlib
import os
import sys

import pytest

import scenarios.scenarios

FAKE_RUNNER = """
import os
import sys

scenario = sys.argv[sys.argv.index("--scenario") + 1]
with open(os.environ["FAKE_RUNNER_LOG"], "a") as log:
    log.write(f"{os.getpid()} {scenario}\\n")
sys.exit(3 if scenario == "broken" else 0)
"""


@pytest.fixture
def runner(tmp_path, monkeypatch):
    # with no meta-scenarios listed, importing the module runs nothing, also on the old code
    monkeypatch.setattr(scenarios.scenarios, "meta_scenarios_list", {})
    monkeypatch.delitem(sys.modules, "run_scenarios", raising=False)
    module = importlib.import_module("run_scenarios")
    script = tmp_path / "fake_runner.py"
    script.write_text(FAKE_RUNNER)
    monkeypatch.setattr(module, "SINGLE_SCENARIO_RUNNER", script)
    log = tmp_path / "calls.txt"
    monkeypatch.setenv("FAKE_RUNNER_LOG", str(log))
    return module, log


def _calls(log):
    return [line.split() for line in log.read_text().splitlines()]


def test_each_meta_scenario_runs_in_its_own_process_in_order(runner):
    module, log = runner
    module.run_all(["first", "second"])
    calls = _calls(log)
    assert [scenario for _, scenario in calls] == ["first", "second"]
    process_ids = {pid for pid, _ in calls}
    assert len(process_ids) == 2 and str(os.getpid()) not in process_ids


def test_failing_meta_scenario_stops_the_batch(runner):
    module, log = runner
    with pytest.raises(SystemExit) as stop:
        module.run_all(["first", "broken", "third"])
    assert "broken" in str(stop.value.code)
    assert [scenario for _, scenario in _calls(log)] == ["first", "broken"]
