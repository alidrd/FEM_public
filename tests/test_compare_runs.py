"""Unit tests for tools/compare_runs.py, the regression check between two runs.

The tests write small run folders by hand: every declared quantity as a long table (key columns plus a
value column, as the export writes them) and statistics.csv with the termination and the objective.
"""
import math

import pandas as pd
import pytest

from tools import compare_runs

ROWS = {("a", "s"): 10.0, ("b", "s"): 0.0, ("c", "s"): 1.0e6}
GEN = next(q for q in compare_runs.QUANTITIES if q.file == "gen.csv")


def write_table(path, rows):
    pd.DataFrame([{"key": k, "Scenarios": s, "value": v} for (k, s), v in rows.items()]).to_csv(path, index=False)


def write_run(folder, objective=1000.0, termination="optimal", solve_time=1.0):
    folder.mkdir(parents=True)
    for quantity in compare_runs.QUANTITIES:
        write_table(folder / quantity.file, ROWS)
    pd.DataFrame({
        "Variable": ["Termination_Condition", "Objective_Value", "solve_time"],
        "Value": [termination, objective, solve_time],
    }).to_csv(folder / "statistics.csv", index=False)
    return folder


def compare(run_a, run_b):
    return compare_runs.main([str(run_a), str(run_b)])


@pytest.fixture
def runs(tmp_path):
    return write_run(tmp_path / "a"), write_run(tmp_path / "b")


def test_equal_runs_pass(runs):
    assert compare(*runs) == 0


def test_row_order_and_solve_time_do_not_matter(tmp_path):
    run_a = write_run(tmp_path / "a")
    run_b = write_run(tmp_path / "b", solve_time=99.0)
    write_table(run_b / "gen.csv", dict(reversed(list(ROWS.items()))))
    assert compare(run_a, run_b) == 0


def test_difference_beyond_tolerance_fails(runs, capsys):
    write_table(runs[1] / "gen.csv", {**ROWS, ("a", "s"): 10.5})
    assert compare(*runs) == 1
    assert "gen.csv" in capsys.readouterr().out


@pytest.mark.parametrize("offset, expected", [(0.5, 0), (2.0, 1)])
def test_relative_tolerance_scales_with_the_value(runs, offset, expected):
    # at 1e6 the allowed difference is abs_tol + rel_tol * 1e6
    assert GEN.abs_tol + GEN.rel_tol * 1.0e6 > 0.5
    write_table(runs[1] / "gen.csv", {**ROWS, ("c", "s"): 1.0e6 + offset})
    assert compare(*runs) == expected


@pytest.mark.parametrize("factor, expected", [(0.5, 0), (2.0, 1)])
def test_near_zero_only_the_absolute_tolerance_applies(runs, factor, expected):
    write_table(runs[1] / "gen.csv", {**ROWS, ("b", "s"): factor * GEN.abs_tol})
    assert compare(*runs) == expected


def test_missing_file_in_one_run_fails(runs, capsys):
    (runs[1] / "Export.csv").unlink()
    assert compare(*runs) == 1
    assert "Export.csv" in capsys.readouterr().out


def test_key_in_one_run_only_fails(runs):
    write_table(runs[1] / "gen.csv", {k: v for k, v in ROWS.items() if k != ("a", "s")})
    assert compare(*runs) == 1


def test_column_mismatch_fails(runs):
    table = pd.read_csv(runs[1] / "gen.csv").rename(columns={"key": "P_gen"})
    table.to_csv(runs[1] / "gen.csv", index=False)
    assert compare(*runs) == 1


def test_duplicate_keys_fail_even_when_both_runs_agree(runs):
    for run in runs:
        table = pd.read_csv(run / "gen.csv")
        pd.concat([table, table.head(1)]).to_csv(run / "gen.csv", index=False)
    assert compare(*runs) == 1


@pytest.mark.parametrize("value", [math.nan, math.inf])
def test_non_finite_values_fail_even_when_both_runs_agree(runs, value):
    for run in runs:
        write_table(run / "gen.csv", {**ROWS, ("a", "s"): value})
    assert compare(*runs) == 1


def test_empty_table_in_both_runs_passes(runs):
    for run in runs:
        pd.DataFrame().to_csv(run / "lostload.csv")  # the export writes an empty component like this
    assert compare(*runs) == 0


def test_objective_beyond_tolerance_fails(tmp_path):
    assert compare(write_run(tmp_path / "a"), write_run(tmp_path / "b", objective=1001.0)) == 1


def test_run_that_did_not_end_optimal_fails(tmp_path):
    assert compare(write_run(tmp_path / "a"), write_run(tmp_path / "b", termination="infeasible")) == 1


def test_same_folder_twice_is_refused(runs):
    assert compare(runs[0], runs[0] / ".." / "a") == 2
