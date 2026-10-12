"""Compare a back-cast's modelled Swiss price with the observed day-ahead price (docs/backcast.md).

Usage:
    python tools/compare_historical_prices.py output/<back-cast run>

The run folder gives energy_balance_dual.csv (CH00 rows), Export.csv, settings.csv, historical_time_map.csv and
historical_inputs_manifest.json; the processed folder the manifest names gives prices_day_ahead_hourly.csv,
ntc_dayahead_combined_hourly.csv and ch_exchanges_scheduled_hourly.csv. Model steps become UTC hours only through the
run's time map, never through their labels. Coverage is exact: every hour of the time map has exactly one CH00 dual,
one flow per border line and one row of every observed table, and every value is finite; anything else raises.

Writes into <run>/price_comparison/:
- metrics.csv: MAE, RMSE, bias (model minus observed) and correlation, for the model and for a benchmark, the mean
  neighbour price; the correlation is undefined for a constant series;
- monthly.csv: mean prices per local month; a month the period cuts is listed but not compared;
- duration_curve.csv: both prices sorted from highest to lowest;
- border_limits.csv: hours each border flow sits at its limit, modelled and observed (scheduled exchange).
"""
import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

LOCAL_TZ = "Europe/Zurich"
NODE = "CH00"
NEIGHBOURS = ("AT00", "DE00", "FR00", "IT00")
# A flow is at its limit within this distance (MW), the rule of data_prep/historical/plot_trade_ntc.py whose shares
# the back-cast plan quotes; the observed data are rounded to 1 MW
TOL_MW = 1.0


def read_time_map(run):
    """The run's time map, indexed by model step; raises unless steps and UTC hours are one-to-one."""
    path = run / "historical_time_map.csv"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: no time map, so the model steps cannot be dated")
    time_map = pd.read_csv(path)
    time_map["time_utc"] = pd.to_datetime(time_map["time_utc"], utc=True)
    for column in ("t", "time_utc"):
        repeated = time_map[column][time_map[column].duplicated()]
        if len(repeated):
            raise ValueError(f"{path}: the time map is not one-to-one, {column} {repeated.iloc[0]} repeats")
    return time_map.set_index("t")


def per_step(table, key, steps, path, what):
    """table indexed by key, exactly one row for each of the steps; raises naming what is missing, repeated or
    outside the time map."""
    repeated = table[key][table[key].duplicated()]
    if len(repeated):
        raise ValueError(f"{path}: {what} repeats for model step {repeated.iloc[0]}")
    outside = sorted(set(table[key]) - set(steps))
    if outside:
        raise ValueError(f"the time map does not cover the model steps of {path}: {len(outside)} steps, "
                         f"e.g. {outside[:3]}")
    missing = [t for t in steps if t not in set(table[key])]
    if missing:
        raise ValueError(f"{path}: no {what} for {len(missing)} model steps of the time map, e.g. {missing[:3]}")
    return table.set_index(key).loc[list(steps)]


def finite(values, path):
    bad = ~np.isfinite(values.to_numpy(dtype=float))
    if bad.any():
        raise ValueError(f"{path}: {int(bad.sum())} missing or non-finite values")
    return values.astype(float)


def read_run(run):
    """Modelled CH price and border flows (positive into CH) by UTC hour, the NTC multiplier, the time map and the
    processed folder of the run."""
    time_map = read_time_map(run)
    manifest = json.loads((run / "historical_inputs_manifest.json").read_text(encoding="utf-8"))
    path = run / "energy_balance_dual.csv"
    dual = pd.read_csv(path)
    dual = dual[dual["Node"] == NODE]
    scenarios = sorted(dual["Scenarios"].unique())
    if len(scenarios) != 1:
        raise ValueError(f"{path}: a back-cast has one sub-scenario, found {scenarios}")
    settings = pd.read_csv(run / "settings.csv", index_col=0)[scenarios[0]]
    weight, ratio = float(settings["weight_in_objective_fcn"]), float(settings["NTC_CH_ratio"])
    hours = pd.DatetimeIndex(time_map["time_utc"])
    # the dual of a sub-scenario is its weight times the price (docs/duals.md)
    model = finite(per_step(dual, "T", time_map.index, path, "CH00 dual")["value"], path) / weight
    path = run / "Export.csv"
    exports = pd.read_csv(path)
    flows = {}
    for n in NEIGHBOURS:
        rows = exports[exports["lineATC"] == f"HVAC_{n}_{NODE}"]
        flows[n] = finite(per_step(rows, "T", time_map.index, path, f"flow on HVAC_{n}_{NODE}")["value"], path)
    flows = pd.DataFrame({n: values.to_numpy() for n, values in flows.items()}, index=hours)
    return pd.Series(model.to_numpy(), index=hours), flows, ratio, time_map, Path(manifest["historical_data_dir"])


def read_observed(folder, name, columns, hours):
    """The given columns of a processed hourly table at the given UTC hours; raises on a missing file, column or
    hour, a repeated hour, or a missing or non-finite value."""
    path = folder / name
    table = pd.read_csv(path)
    absent = [c for c in ["time_utc", *columns] if c not in table.columns]
    if absent:
        raise ValueError(f"{path}: no column {absent}")
    table.index = pd.to_datetime(table["time_utc"], utc=True)
    if table.index.duplicated().any():
        raise ValueError(f"{path}: hour {table.index[table.index.duplicated()][0]} repeats")
    missing = hours.difference(table.index)
    if len(missing):
        raise ValueError(f"{path}: {len(missing)} hours of the run are missing, e.g. {missing[0]}")
    return finite(table.loc[hours, columns], path)


def metrics(predicted, observed):
    error = predicted - observed
    constant = predicted.std() == 0 or observed.std() == 0
    return {"MAE": float(error.abs().mean()), "RMSE": math.sqrt(float((error ** 2).mean())),
            "bias": float(error.mean()),
            "correlation": None if constant else float(np.corrcoef(predicted, observed)[0, 1])}


def monthly_means(prices):
    """Mean model and observed price per local month; a month whose hours are not all in the period is listed
    with complete False and no difference."""
    local = prices.index.tz_convert(LOCAL_TZ)
    rows = []
    for month, group in prices.groupby(local.strftime("%Y-%m")):
        start = pd.Timestamp(f"{month}-01").tz_localize(LOCAL_TZ)
        end = (pd.Timestamp(f"{month}-01") + pd.offsets.MonthBegin(1)).tz_localize(LOCAL_TZ)
        complete = len(group) == int((end - start) / pd.Timedelta(hours=1))
        model, observed = group["model"].mean(), group["observed"].mean()
        rows.append({"month": month, "hours": len(group), "complete": complete, "model": model,
                     "observed": observed, "difference": model - observed if complete else math.nan})
    return pd.DataFrame(rows)


def duration_curve(prices):
    model = np.sort(prices["model"].to_numpy())[::-1]
    observed = np.sort(prices["observed"].to_numpy())[::-1]
    rank = np.arange(1, len(model) + 1)
    return pd.DataFrame({"rank": rank, "share_of_hours": rank / len(model), "model": model, "observed": observed})


def border_limits(flows, ratio, ntc, scheduled):
    """Hours each border flow is within TOL_MW of its limit: modelled flows against the model's limits (NTC times
    NTC_CH_ratio), the scheduled exchange against the NTC."""
    rows = []
    for n in NEIGHBOURS:
        c = n[:2]
        for direction, sign, limit in (("import", 1, f"{c}_to_CH"), ("export", -1, f"CH_to_{c}")):
            modelled = ((sign * flows[n] - ratio * ntc[limit]).abs() <= TOL_MW)
            observed = ((sign * scheduled[n] - ntc[limit]).abs() <= TOL_MW)
            rows.append({"border": f"CH-{c}", "direction": direction, "hours": len(flows),
                         "modelled_at_limit": int(modelled.sum()), "modelled_share": float(modelled.mean()),
                         "observed_at_limit": int(observed.sum()), "observed_share": float(observed.mean())})
    return pd.DataFrame(rows)


def compare(run):
    """All tables of the comparison for one run folder."""
    model, flows, ratio, time_map, folder = read_run(run)
    hours = model.index
    prices = read_observed(folder, "prices_day_ahead_hourly.csv", [NODE, *NEIGHBOURS], hours)
    paired = pd.DataFrame({"model": model, "observed": prices[NODE]})
    if len(paired) != len(time_map):
        raise ValueError(f"paired {len(paired)} hours, expected {len(time_map)}")
    benchmark = prices[list(NEIGHBOURS)].mean(axis=1)
    ntc = read_observed(folder, "ntc_dayahead_combined_hourly.csv",
                        [f"{n[:2]}_to_CH" for n in NEIGHBOURS] + [f"CH_to_{n[:2]}" for n in NEIGHBOURS], hours)
    scheduled = read_observed(folder, "ch_exchanges_scheduled_hourly.csv", list(NEIGHBOURS), hours)
    table = pd.DataFrame([{"series": "model", **metrics(paired["model"], paired["observed"])},
                          {"series": "benchmark: mean neighbour price", **metrics(benchmark, paired["observed"])}])
    return {"expected_hours": len(time_map), "paired_hours": len(paired), "metrics": table,
            "monthly": monthly_means(paired), "duration_curve": duration_curve(paired),
            "border_limits": border_limits(flows, ratio, ntc, scheduled)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="run folder of a back-cast, e.g. output/hist_2526_octaug")
    run = parser.parse_args(argv).run
    result = compare(run)
    out = run / "price_comparison"
    out.mkdir(exist_ok=True)
    for name in ("metrics", "monthly", "duration_curve", "border_limits"):
        result[name].to_csv(out / f"{name}.csv", index=False)
    print(f"{run}: {result['paired_hours']} of {result['expected_hours']} hours paired (EUR/MWh)")
    print(result["metrics"].to_string(index=False))
    print(result["monthly"].to_string(index=False))
    print(result["border_limits"].to_string(index=False))
    print(f"tables written to {out}")
    return result


if __name__ == "__main__":
    main(sys.argv[1:])
