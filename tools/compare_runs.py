"""Compare the results of two runs (folders under output/) within declared tolerances.

Usage:
    python tools/compare_runs.py output/<run_a> output/<run_b>

Each declared quantity is one exported table (key columns plus `value`). Rows are matched by their keys, so
row order does not matter; the keys include the sub-scenario name, so both runs must come from the same
scenario. Two values agree when |a - b| <= abs_tol + rel_tol * max(|a|, |b|). A file,
key or column present in only one run, a duplicate key and a missing or non-finite value count as
differences, also when both runs agree. The objective comes from statistics.csv, and both runs must have
ended optimal; solver timings are not compared.

Exit code 0: every quantity within tolerance; 1: differences; 2: the arguments are not two run folders.
"""
import argparse
import math
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class Quantity:
    file: str
    abs_tol: float  # in the unit of the file: MW, MWh or EUR/MWh
    rel_tol: float


# The declared quantities and their tolerances, all in one place
QUANTITIES = [
    # capacities
    Quantity("gen_max.csv", 1e-3, 1e-6),
    Quantity("pmp_max.csv", 1e-3, 1e-6),
    Quantity("gen_energy_max.csv", 1e-3, 1e-6),
    Quantity("genTh_max.csv", 1e-3, 1e-6),
    # dispatch
    Quantity("gen.csv", 1e-3, 1e-6),
    Quantity("storage_charge.csv", 1e-3, 1e-6),
    Quantity("Export.csv", 1e-3, 1e-6),
    Quantity("lostload.csv", 1e-3, 1e-6),
    Quantity("curtailment.csv", 1e-3, 1e-6),
    Quantity("genTh.csv", 1e-3, 1e-6),
    # prices
    Quantity("energy_balance_dual.csv", 1e-3, 1e-6),
    Quantity("energy_balancethermal_dual.csv", 1e-3, 1e-6),
]
OBJECTIVE = Quantity("statistics.csv", 1e-2, 1e-7)


def read_table(path):
    """The exported table; the export writes an empty component as a file without rows."""
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def compare_quantity(quantity, run_a, run_b):
    """Differences of one declared table, and a one-line summary."""
    paths = (run_a / quantity.file, run_b / quantity.file)
    present = [path.exists() for path in paths]
    if not all(present):
        where = "both runs" if not any(present) else ("run B" if present[0] else "run A")
        return [f"{quantity.file}: missing in {where}"], "missing"
    table_a, table_b = (read_table(path) for path in paths)
    if len(table_a) == 0 and len(table_b) == 0:
        return [], "empty in both runs"
    if list(table_a.columns) != list(table_b.columns):
        return [f"{quantity.file}: columns differ, {list(table_a.columns)} vs {list(table_b.columns)}"], "columns differ"
    if "value" not in table_a.columns:
        return [f"{quantity.file}: no value column"], "no value column"

    keys = [column for column in table_a.columns if column != "value"]
    problems = []
    values = {}
    for label, table in (("A", table_a), ("B", table_b)):
        duplicates = int(table.duplicated(subset=keys).sum())
        if duplicates:
            problems.append(f"{quantity.file}: {duplicates} duplicate keys in run {label}")
        numbers = pd.to_numeric(table["value"], errors="coerce")
        not_finite = int((numbers.isna() | numbers.isin([math.inf, -math.inf])).sum())
        if not_finite:
            problems.append(f"{quantity.file}: {not_finite} missing or non-finite values in run {label}")
        values[label] = pd.Series(numbers.to_numpy(), index=pd.MultiIndex.from_frame(table[keys].astype(str)))
    if problems:
        return problems, "invalid table"

    for label, other in (("A", "B"), ("B", "A")):
        only_here = values[label].index.difference(values[other].index)
        if len(only_here):
            problems.append(f"{quantity.file}: {len(only_here)} keys only in run {label}, e.g. {list(only_here[:3])}")
    common = values["A"].index.intersection(values["B"].index)
    value_a, value_b = values["A"].loc[common], values["B"].loc[common]
    difference = (value_a - value_b).abs()
    scale = pd.concat([value_a.abs(), value_b.abs()], axis=1).max(axis=1)
    beyond = difference > quantity.abs_tol + quantity.rel_tol * scale
    if beyond.any():
        worst = difference[beyond].idxmax()
        problems.append(
            f"{quantity.file}: {int(beyond.sum())} values beyond tolerance, largest |difference| "
            f"{difference.max():.6g} at {worst}"
        )
    largest_abs = difference.max() if len(difference) else 0.0
    relative = (difference / scale).where(scale > 0, 0.0)
    largest_rel = relative.max() if len(relative) else 0.0
    return problems, f"max |diff| {largest_abs:.3g}, max rel {largest_rel:.3g}"


def compare_objective(run_a, run_b):
    """Differences of the objective, which both runs must have reached optimally."""
    problems = []
    objectives = {}
    for label, run in (("A", run_a), ("B", run_b)):
        path = run / OBJECTIVE.file
        if not path.exists():
            return [f"{OBJECTIVE.file}: missing in run {label}"], "missing"
        statistics = read_table(path).set_index("Variable")["Value"]
        termination = statistics["Termination_Condition"] if "Termination_Condition" in statistics.index else "missing"
        if termination != "optimal":
            problems.append(f"run {label} ended {termination}, not optimal")
        objective = pd.to_numeric(statistics["Objective_Value"], errors="coerce") if "Objective_Value" in statistics.index else math.nan
        if not math.isfinite(objective):
            problems.append(f"{OBJECTIVE.file}: the objective of run {label} is missing or not finite")
        objectives[label] = objective
    if problems:
        return problems, "invalid"
    difference = abs(objectives["A"] - objectives["B"])
    scale = max(abs(objectives["A"]), abs(objectives["B"]))
    if difference > OBJECTIVE.abs_tol + OBJECTIVE.rel_tol * scale:
        problems.append(f"objective: |difference| {difference:.6g}, A {objectives['A']:.10g}, B {objectives['B']:.10g}")
    return problems, f"|diff| {difference:.3g}"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare the results of two runs within declared tolerances.")
    parser.add_argument("run_a", type=Path, help="run folder, e.g. output/baselines/2050_st")
    parser.add_argument("run_b", type=Path, help="run folder, e.g. output/2050_st")
    args = parser.parse_args(argv)
    for run in (args.run_a, args.run_b):
        if not run.is_dir():
            print(f"Not a run folder: {run}", file=sys.stderr)
            return 2
    if args.run_a.resolve() == args.run_b.resolve():
        print(f"Both arguments name the same folder: {args.run_a.resolve()}", file=sys.stderr)
        return 2

    print(f"A: {args.run_a}\nB: {args.run_b}")
    rows = [(quantity.file, *compare_quantity(quantity, args.run_a, args.run_b)) for quantity in QUANTITIES]
    rows.append(("objective", *compare_objective(args.run_a, args.run_b)))
    problems = []
    for name, found, summary in rows:
        print(f"{'DIFF' if found else 'ok  '} {name:<32} {summary}")
        problems.extend(found)
    if problems:
        print(f"\n{len(problems)} differences beyond the declared tolerances:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print("\nAll declared quantities agree within tolerance.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
