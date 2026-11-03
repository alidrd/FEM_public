"""Investment cost of per-plant rooftop-PV candidates by cost class.

The scenario setting `pv_roof_cost_mode` decides what a per-plant candidate
(`high_resolution_PV` on) pays per MW and year:

    tech      the technology cost of `pvrf` from input/cost_assumptions.xlsx (default)
    by_class  the cost of the unit's `cost_class` (column of units.csv) from COST_TABLE
    average   the `all` row of COST_TABLE for every unit

COST_TABLE holds overnight costs in CHF per MW by class and run year; they are
annuitised with FEM's rule (`utils/annuity`) over the `pvrf` amortization
period, so a class and the `pvrf` technology are priced on the same terms.
Anything missing is an error, never a default. Guide: docs/high_res_pv.md.
"""
import os

import pandas as pd

import input.cost_operation_invest_data as cost_params
from utils.annuity import DISCOUNT_RATE, annuitise

COST_TABLE = os.path.join("input", "cost_pv_roof_cost_classes.csv")
COST_TABLE_COLUMNS = ("cost_class", "year", "investment_cost_chfMW")
COST_MODES = ("tech", "by_class", "average")
AVERAGE_CLASS = "all"
PV_ROOF_TECH = "pvrf"   # the technology whose amortization period the classes share


def validate_cost_mode(mode, high_resolution_PV):
    """Return `mode` if it is a known mode and usable with the flag; raise otherwise."""
    if mode not in COST_MODES:
        raise ValueError(
            f"[pv_roof_costs] pv_roof_cost_mode={mode!r} is not one of {COST_MODES}."
        )
    if mode != "tech" and not high_resolution_PV:
        raise ValueError(
            f"[pv_roof_costs] pv_roof_cost_mode={mode!r} needs high_resolution_PV=True; "
            "the aggregated CHxx_pvrf candidates have no cost class."
        )
    return mode


def read_cost_table(path=COST_TABLE):
    """Read the class cost table; raise if a column is missing or a (class, year) repeats."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"[pv_roof_costs] cost table {path} not found.")
    table = pd.read_csv(path)
    missing = [c for c in COST_TABLE_COLUMNS if c not in table.columns]
    if missing:
        raise ValueError(f"[pv_roof_costs] {path} lacks column(s) {missing}; expected {list(COST_TABLE_COLUMNS)}.")
    duplicated = table.duplicated(subset=["cost_class", "year"], keep=False)
    if duplicated.any():
        pairs = table.loc[duplicated, ["cost_class", "year"]].drop_duplicates().values.tolist()
        raise ValueError(f"[pv_roof_costs] {path} repeats (cost_class, year) {pairs}.")
    table["year"] = table["year"].astype(int)
    return table


def class_investment_slopes(table, run_year, amortization_years=None, discount_rate=DISCOUNT_RATE):
    """Annuitised cost per MW and year of every class the table has for `run_year`."""
    if amortization_years is None:
        amortization_years = cost_params.amortization_years_all[PV_ROOF_TECH]
    rows = table[table["year"] == int(run_year)]
    if rows.empty:
        raise ValueError(
            f"[pv_roof_costs] no cost-class rows for run_year={run_year}; "
            f"years in the table: {sorted(table['year'].unique().tolist())}."
        )
    return {
        row.cost_class: annuitise(float(row.investment_cost_chfMW), amortization_years, discount_rate)
        for row in rows.itertuples()
    }


def plant_cost_classes(mode, unit_cost_classes):
    """The class each per-plant candidate is priced at, {plant_id: class}; {} in mode `tech`.

    `unit_cost_classes` is what `model.high_res_PV.load_cost_classes` returns:
    {plant_id: class} when units.csv has the column, None when it does not.
    """
    if mode == "tech":
        return {}
    if unit_cost_classes is None:
        raise ValueError(
            f"[pv_roof_costs] pv_roof_cost_mode={mode!r} but units.csv has no `cost_class` column; "
            "rewrite the unit table with data_prep/export_nexuse_pv.py --units-only --cost-class ..."
        )
    if mode == "average":
        return {plant: AVERAGE_CLASS for plant in unit_cost_classes}
    return dict(unit_cost_classes)


def plant_investment_slopes(plant_classes, run_year, table=None):
    """{plant_id: CHF per MW and year} for every plant in `plant_classes`; raise on an unknown class."""
    if not plant_classes:
        return {}
    if table is None:
        table = read_cost_table()
    by_class = class_investment_slopes(table, run_year)
    unknown = sorted({cls for cls in plant_classes.values() if cls not in by_class})
    if unknown:
        raise ValueError(
            f"[pv_roof_costs] cost class(es) {unknown} of the units have no row for run_year={run_year} "
            f"in {COST_TABLE}; classes there: {sorted(by_class)}."
        )
    return {plant: by_class[cls] for plant, cls in plant_classes.items()}
