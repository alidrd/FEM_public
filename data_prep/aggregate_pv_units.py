"""Aggregate a per-unit rooftop-PV dataset to one unit per bus, written in the same format as the source.

Each bus gets one unit whose Pmax is the sum of the Pmax of the units kept there and whose profile is the sum of
their hourly output, each unit clipped to its Pmax as FEM does: the potential-weighted average availability, i.e.
building every kept unit in proportion to its potential. Kept units are all PV-roof units with Pmax > 0, or, with
--potential-mw, only the highest-yield units nationally until that much potential is reached (one national
full-load-hour threshold). The run year must be the only profile year of the source.

usage: python data_prep/aggregate_pv_units.py <source dataset> <target dataset> <weather year> <run year>
                                              [--potential-mw <MW>]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from model.high_res_PV import HOUR_COLS, PROFILES_FILE, PV_DIR, PV_ROOF_TECHNOLOGY, UNITS_FILE  # noqa: E402

NODE_SUFFIX = "node"


def read_dataset(folder: str, run_year: int) -> tuple[pd.DataFrame, np.ndarray]:
    """PV-roof units with Pmax > 0 (GenName, Pmax, BusName) and their hourly output in MW, clipped to Pmax."""
    units = pd.read_csv(os.path.join(folder, UNITS_FILE))
    missing = [c for c in ("GenName", "Pmax", "Technology", "BusName") if c not in units.columns]
    if missing:
        raise ValueError(f"{folder}/{UNITS_FILE} lacks column(s) {missing}")
    units = units[(units["Technology"] == PV_ROOF_TECHNOLOGY) & (units["Pmax"] > 0)].reset_index(drop=True)
    if units["BusName"].isna().any():
        raise ValueError(f"{folder}: {int(units['BusName'].isna().sum())} PV-roof units without a bus")
    profiles = pd.read_csv(os.path.join(folder, PROFILES_FILE), usecols=["Name", "year"] + HOUR_COLS)
    years = sorted(int(y) for y in profiles["year"].unique())
    if years != [int(run_year)]:
        raise ValueError(f"{folder}: profile years {years}; expected only {run_year}")
    profiles = profiles.set_index("Name")
    absent = sorted(set(units["GenName"]) - set(profiles.index))
    if absent:
        raise ValueError(f"{folder}: no profile for {len(absent)} unit(s), e.g. {absent[:3]}")
    output = profiles.loc[units["GenName"], HOUR_COLS].to_numpy(dtype=float)
    return units, np.minimum(output, units["Pmax"].to_numpy(dtype=float)[:, None])


def keep_best_until(full_load_hours: np.ndarray, pmax: np.ndarray, potential_mw: float) -> tuple[np.ndarray, float]:
    """Units with the highest full-load hours until their Pmax first reaches potential_mw, and the hours of the
    last unit kept (the national threshold)."""
    if potential_mw <= 0 or potential_mw > pmax.sum():
        raise ValueError(f"potential {potential_mw} MW is not within (0, {pmax.sum():.1f}] MW")
    order = np.argsort(-full_load_hours, kind="stable")
    reached = np.cumsum(pmax[order]) >= potential_mw
    last = int(np.argmax(reached))
    keep = np.zeros(len(pmax), dtype=bool)
    keep[order[: last + 1]] = True
    return keep, float(full_load_hours[order[last]])


def aggregate_by_bus(units: pd.DataFrame, output: np.ndarray, keep: np.ndarray) -> tuple[pd.DataFrame, np.ndarray]:
    """One unit per bus among the kept units: summed Pmax and summed hourly output."""
    kept = units.loc[keep, ["BusName", "Pmax"]].reset_index(drop=True)
    kept_output = output[keep]
    buses = sorted(kept["BusName"].unique())
    rows, sums = [], []
    for bus in buses:
        members = (kept["BusName"] == bus).to_numpy()
        rows.append({"GenName": f"CH_RES_PV_{bus}_{NODE_SUFFIX}", "Pmax": kept.loc[members, "Pmax"].sum(),
                     "Technology": PV_ROOF_TECHNOLOGY, "BusName": bus, "cost_class": "all",
                     "n_units": int(members.sum())})
        sums.append(kept_output[members].sum(axis=0))
    return pd.DataFrame(rows), np.vstack(sums)


def write_dataset(folder: str, units: pd.DataFrame, output: np.ndarray, run_year: int, source_lines: list[str]) -> None:
    """units.csv, the profile file (MWh per hour at Pmax) and SOURCE.md; refuses an existing folder."""
    if os.path.exists(folder):
        raise FileExistsError(f"{folder} exists already; remove it first if it is to be rebuilt")
    os.makedirs(folder)
    units.to_csv(os.path.join(folder, UNITS_FILE), index=False)
    profiles = pd.DataFrame(output, columns=HOUR_COLS)
    profiles.insert(0, "year", int(run_year))
    profiles.insert(0, "Name", units["GenName"].to_numpy())
    profiles.to_csv(os.path.join(folder, PROFILES_FILE), index=False)
    with open(os.path.join(folder, "SOURCE.md"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# Source of this dataset\n\n" + "".join(f"- {line}\n" for line in source_lines))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source")
    parser.add_argument("target")
    parser.add_argument("weather_year", type=int)
    parser.add_argument("run_year", type=int)
    parser.add_argument("--potential-mw", type=float, default=None,
                        help="keep only the highest-yield units nationally until this much potential is reached")
    args = parser.parse_args(argv)
    source = os.path.join(PV_DIR, args.source, str(args.weather_year))
    target = os.path.join(PV_DIR, args.target, str(args.weather_year))
    units, output = read_dataset(source, args.run_year)
    pmax = units["Pmax"].to_numpy(dtype=float)
    flh = output.sum(axis=1) / pmax
    if args.potential_mw is None:
        keep, rule = np.ones(len(units), dtype=bool), "all PV-roof units with Pmax > 0"
    else:
        keep, threshold = keep_best_until(flh, pmax, args.potential_mw)
        rule = (f"only the highest-yield units nationally until {args.potential_mw:,.0f} MW of potential "
                f"(annual full-load hours at least {threshold:,.1f})")
    agg_units, agg_output = aggregate_by_bus(units, output, keep)
    energy = agg_output.sum() / 1e6
    lines = [f"Aggregated {date.today().isoformat()} by data_prep/aggregate_pv_units.py from dataset `{args.source}` "
             f"(weather year {args.weather_year}, profile year {args.run_year})",
             f"Kept: {rule}: {int(keep.sum())} of {len(units)} units, {pmax[keep].sum():,.1f} of {pmax.sum():,.1f} MW",
             "One unit per bus: Pmax = sum of the kept units' Pmax; profile = sum of their hourly output, each clipped "
             "to its Pmax (the potential-weighted average availability)",
             f"Result: {len(agg_units)} units on {agg_units['BusName'].nunique()} buses, sum Pmax "
             f"{agg_units['Pmax'].sum():,.1f} MW, energy at Pmax {energy:.2f} TWh "
             f"({energy * 1e6 / agg_units['Pmax'].sum():,.0f} full-load hours); cost_class `all`"]
    write_dataset(target, agg_units, agg_output, args.run_year, lines)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
