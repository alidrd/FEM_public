"""The pytest configuration for tests that need local data (marker needs_data).

Plain `poetry run pytest`, as on CI, must deselect them and collect only tests/; `-m needs_data` selects
them; and a selected test whose data are missing fails, naming the paths, instead of skipping. The
configuration checks run pytest in a subprocess with the repository's pyproject.toml.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MARKED_TEST = '''
import pytest


@pytest.mark.needs_data
def test_marked():
    raise AssertionError("the marked test ran")
'''


def run_pytest(*args):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-c", str(REPO_ROOT / "pyproject.toml"), *args],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    return result.stdout + result.stderr


def test_needs_data_marker_is_registered():
    assert "@pytest.mark.needs_data" in run_pytest("--markers")


def test_marked_test_is_deselected_by_default_and_runs_when_selected(tmp_path):
    test_file = tmp_path / "test_marked.py"
    test_file.write_text(MARKED_TEST)
    assert "1 deselected" in run_pytest(str(test_file))
    assert "the marked test ran" in run_pytest(str(test_file), "-m", "needs_data")


def test_collection_is_limited_to_the_tests_folder():
    collected = [line for line in run_pytest("--collect-only", "-q").splitlines() if "::" in line]
    assert collected and all(line.startswith("tests/") for line in collected)


def test_missing_local_data_fails_naming_the_path():
    from tests.local_data import require_local_data

    # catch any outcome, so that a skip instead of a failure makes this test fail rather than skip
    with pytest.raises(BaseException) as outcome:
        require_local_data("input", "no_such_folder/table.csv")
    assert outcome.type is pytest.fail.Exception
    assert "no_such_folder" in str(outcome.value)
    assert require_local_data("input") == [REPO_ROOT / "input"]
