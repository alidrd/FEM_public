"""Unit tests for tools/compare_historical_prices.py on small hand-made back-cast run folders and processed tables.

The modelled CH00 dual of each model step equals the observed CH price of the UTC hour the time map gives it, plus a
fixed offset, so any error in the mapping from model steps to hours shows as a non-constant error.
"""
import json
import math

import numpy as np
import pandas as pd
import pytest

from data_prep.historical_import import historical_time_map
from tools import compare_historical_prices as tool

NAME = "bc_obs"
NEIGHBOURS = ("AT00", "DE00", "FR00", "IT00")


def make_run(tmp_path, period, offset=2.0, ratio=1.0, weight=1.0):
    """A run folder and a processed folder for the period. Observed CH price: 50 + 0.5 k in hour k (one hour of
    margin each side); neighbours 40, 50, 60, 70; NTC 1000 MW into and 500 MW out of CH; scheduled exchange 1000 MW
    into CH from FR (at the limit), 0 elsewhere; modelled flows equal the scheduled exchange."""
    run, processed = tmp_path / "run", tmp_path / "processed"
    run.mkdir()
    processed.mkdir()
    time_map = historical_time_map(period)
    time_map.to_csv(run / "historical_time_map.csv", index=False)
    (run / "historical_inputs_manifest.json").write_text(json.dumps({"historical_data_dir": str(processed)}))
    pd.DataFrame({"Item": ["NTC_CH_ratio", "weight_in_objective_fcn"], NAME: [ratio, weight]}).to_csv(
        run / "settings.csv", index=False)
    hours = pd.date_range(time_map["time_utc"].iloc[0] - pd.Timedelta(hours=1),
                          time_map["time_utc"].iloc[-1] + pd.Timedelta(hours=1), freq="h")
    observed = pd.Series(50.0 + 0.5 * np.arange(len(hours)), index=hours)
    prices = {"time_utc": hours, "CH00": observed.to_numpy()}
    prices.update({n: 40.0 + 10 * i for i, n in enumerate(NEIGHBOURS)})
    pd.DataFrame(prices).to_csv(processed / "prices_day_ahead_hourly.csv", index=False)
    ntc = {"time_utc": hours}
    ntc.update({f"{n[:2]}_to_CH": 1000.0 for n in NEIGHBOURS})
    ntc.update({f"CH_to_{n[:2]}": 500.0 for n in NEIGHBOURS})
    pd.DataFrame(ntc).to_csv(processed / "ntc_dayahead_combined_hourly.csv", index=False)
    scheduled = {"time_utc": hours, **{n: (1000.0 if n == "FR00" else 0.0) for n in NEIGHBOURS}}
    pd.DataFrame(scheduled).to_csv(processed / "ch_exchanges_scheduled_hourly.csv", index=False)
    by_step = time_map.assign(price=observed.loc[time_map["time_utc"]].to_numpy())
    # rows in label order, which is not time order (t_1000 before t_2): the tool must not rely on row order
    by_step = by_step.sort_values("t")
    dual = pd.DataFrame({"T": by_step["t"], "Node": "CH00", "Scenarios": NAME,
                         "value": weight * (by_step["price"] + offset)})
    dual.to_csv(run / "energy_balance_dual.csv", index=False)
    pd.concat([pd.DataFrame({"lineATC": f"HVAC_{n}_CH00", "T": by_step["t"], "Scenarios": NAME,
                             "value": ratio * (1000.0 if n == "FR00" else 0.0)}) for n in NEIGHBOURS]).to_csv(
        run / "Export.csv", index=False)
    return run, processed


def rewrite(path, change):
    change(pd.read_csv(path)).to_csv(path, index=False)


# --- metrics ------------------------------------------------------------------------------------------------------

def test_metrics_match_hand_values():
    result = tool.metrics(pd.Series([1.0, 2.0, 3.0]), pd.Series([2.0, 2.0, 5.0]))  # errors -1, 0, -2
    assert result["MAE"] == pytest.approx(1.0) and result["RMSE"] == pytest.approx(math.sqrt(5 / 3))
    assert result["bias"] == pytest.approx(-1.0) and result["correlation"] == pytest.approx(3 / math.sqrt(12))


def test_a_constant_series_has_no_correlation():
    result = tool.metrics(pd.Series([4.0, 4.0, 4.0]), pd.Series([2.0, 2.0, 5.0]))
    assert result["correlation"] is None and result["MAE"] == pytest.approx(5 / 3)


# --- time mapping -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("period, hours", [
    ("2025-12-31/2026-01-01", 48),  # model steps t_8737 ... t_8760, t_1 ... t_24
    ("2025-10-26/2025-10-26", 25),  # the autumn day has two 02:00 local hours
])
def test_each_model_step_meets_the_price_of_its_own_hour(tmp_path, period, hours):
    run, _ = make_run(tmp_path, period, offset=2.0)
    result = tool.compare(run)
    assert result["expected_hours"] == result["paired_hours"] == hours
    model = result["metrics"].set_index("series").loc["model"]
    assert model["bias"] == pytest.approx(2.0) and model["MAE"] == pytest.approx(2.0)
    assert model["RMSE"] == pytest.approx(2.0) and model["correlation"] == pytest.approx(1.0)


def test_the_benchmark_is_the_mean_neighbour_price(tmp_path):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01")
    observed = 50.0 + 0.5 * np.arange(1, 25)  # the hours of the period, after the hour of margin
    benchmark = tool.compare(run)["metrics"].set_index("series").loc["benchmark: mean neighbour price"]
    assert benchmark["bias"] == pytest.approx(np.mean(55.0 - observed))  # mean of 40, 50, 60, 70
    assert benchmark["MAE"] == pytest.approx(np.mean(np.abs(55.0 - observed)))
    assert math.isnan(benchmark["correlation"])  # undefined: the neighbour prices are constant


def test_the_weight_of_the_sub_scenario_is_divided_out(tmp_path):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01", offset=0.0, weight=0.5)
    assert tool.compare(run)["metrics"].set_index("series").at["model", "MAE"] == pytest.approx(0.0)


def test_missing_time_map_raises(tmp_path):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01")
    (run / "historical_time_map.csv").unlink()
    with pytest.raises(FileNotFoundError, match="historical_time_map.csv not found"):
        tool.compare(run)


@pytest.mark.parametrize("change, message", [
    (lambda f: pd.concat([f, f.iloc[[0]].assign(T="t_9")]), "the time map does not cover the model steps"),
    (lambda f: f.iloc[1:], "no CH00 dual for 1 model steps"),
    (lambda f: pd.concat([f, f.iloc[[3]]]), "CH00 dual repeats"),
    (lambda f: f.assign(value=f["value"].where(f.index != 5)), "missing or non-finite"),
    (lambda f: pd.concat([f, f.assign(Scenarios="other")]), "one sub-scenario"),
])
def test_an_incomplete_or_inconsistent_dual_raises(tmp_path, change, message):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01")
    rewrite(run / "energy_balance_dual.csv", change)
    with pytest.raises(ValueError, match=message):
        tool.compare(run)


def test_a_time_map_that_is_not_one_to_one_raises(tmp_path):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01")
    rewrite(run / "historical_time_map.csv", lambda f: f.assign(time_utc=f["time_utc"].where(f.index != 3,
                                                                                            f["time_utc"].iloc[2])))
    with pytest.raises(ValueError, match="not one-to-one"):
        tool.compare(run)


@pytest.mark.parametrize("table, change, message", [
    ("prices_day_ahead_hourly.csv", lambda f: f.drop(index=4), "1 hours of the run are missing"),
    ("prices_day_ahead_hourly.csv", lambda f: f.assign(FR00=f["FR00"].where(f.index != 4)), "non-finite"),
    ("ntc_dayahead_combined_hourly.csv", lambda f: f.drop(columns="CH_to_IT"), r"no column \['CH_to_IT'\]"),
    ("ch_exchanges_scheduled_hourly.csv", lambda f: pd.concat([f, f.iloc[[4]]]), "repeats"),
])
def test_an_incomplete_observation_table_raises(tmp_path, table, change, message):
    run, processed = make_run(tmp_path, "2025-10-01/2025-10-01")
    rewrite(processed / table, change)
    with pytest.raises(ValueError, match=message):
        tool.compare(run)


# --- tables -------------------------------------------------------------------------------------------------------

def test_a_month_the_period_cuts_is_listed_but_not_compared(tmp_path):
    run, _ = make_run(tmp_path, "2026-02-01/2026-03-01")  # all of February, one day of March
    monthly = tool.compare(run)["monthly"].set_index("month")
    assert monthly.at["2026-02", "hours"] == 672 and monthly.at["2026-02", "complete"]
    assert monthly.at["2026-02", "difference"] == pytest.approx(2.0)
    assert monthly.at["2026-03", "hours"] == 24 and not monthly.at["2026-03", "complete"]
    assert math.isnan(monthly.at["2026-03", "difference"])


def test_duration_curve_sorts_both_prices_from_highest(tmp_path):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01")
    curve = tool.compare(run)["duration_curve"]
    assert curve["model"].is_monotonic_decreasing and curve["observed"].is_monotonic_decreasing
    assert (curve["model"] - curve["observed"]).tolist() == pytest.approx([2.0] * 24)
    assert curve["share_of_hours"].iloc[-1] == 1.0


@pytest.mark.parametrize("ratio", [1.0, 0.5])
def test_hours_at_the_border_limits(tmp_path, ratio):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01", ratio=ratio)  # model flow FR = ratio * 1000 = its limit
    limits = tool.compare(run)["border_limits"].set_index(["border", "direction"])
    assert limits.loc[("CH-FR", "import"), ["modelled_at_limit", "observed_at_limit"]].tolist() == [24, 24]
    assert limits["modelled_at_limit"].sum() == 24 and limits["observed_at_limit"].sum() == 24


def test_the_command_writes_the_tables(tmp_path, capsys):
    run, _ = make_run(tmp_path, "2025-10-01/2025-10-01")
    tool.main([str(run)])
    assert sorted(p.name for p in (run / "price_comparison").iterdir()) == [
        "border_limits.csv", "duration_curve.csv", "metrics.csv", "monthly.csv"]
    assert "24 of 24 hours paired" in capsys.readouterr().out
