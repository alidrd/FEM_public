"""Small processed folders for back-cast tests: the tables of data_prep/historical, for one short period plus one
hour on each side, in the pipeline's format (index column time_utc, UTC interval start)."""
import pandas as pd

from data_prep.historical_import import BORDER_NEIGHBOURS, historical_time_map

# Asymmetric transfer limits per neighbour (MW), so that a reversed direction shows
INTO_CH = {"AT00": 1200.0, "DE00": 1150.0, "FR00": 2015.0, "IT00": 1910.0}
OUT_OF_CH = {"AT00": 630.0, "DE00": 4000.0, "FR00": 900.0, "IT00": 3716.0}


def period_hours(period):
    hours = historical_time_map(period)["time_utc"]
    return pd.DatetimeIndex([hours.iloc[0] - pd.Timedelta(hours=1), *hours, hours.iloc[-1] + pd.Timedelta(hours=1)])


def write_processed(folder, period):
    """Write the processed tables a back-cast reads. Prices rise by 1 EUR/MWh per hour from 50 (DE00 from -5);
    demand_MW rises by 1 MW per hour from 6000, with the solar-shaped column 100 MW below and the flat one 100 MW
    above; every month is published definitively. Solar, wind and run-of-river rise by 1 MW per hour from 100, 10
    and 1000."""
    folder.mkdir(parents=True, exist_ok=True)
    hours = period_hours(period)
    steps = range(len(hours))
    prices = {"time_utc": hours, "CH00": 80.0}
    for n in BORDER_NEIGHBOURS:
        prices[n] = [(-5.0 if n == "DE00" else 50.0) + k for k in steps]
    pd.DataFrame(prices).to_csv(folder / "prices_day_ahead_hourly.csv", index=False, float_format="%.3f")
    ntc = {"time_utc": hours}
    for n in BORDER_NEIGHBOURS:
        ntc[f"CH_to_{n[:2]}"] = OUT_OF_CH[n]
        ntc[f"{n[:2]}_to_CH"] = INTO_CH[n]
    for direction in [column for column in ntc if column != "time_utc"]:
        ntc[f"source_{direction}"] = "swissgrid_d2"
    pd.DataFrame(ntc).to_csv(folder / "ntc_dayahead_combined_hourly.csv", index=False, float_format="%.3f")
    demand = [6000.0 + k for k in steps]
    pd.DataFrame({"time_utc": hours, "demand_MW": demand, "demand_solar_shaped_MW": [d - 100 for d in demand],
                  "demand_flat_MW": [d + 100 for d in demand], "onsite_solar_share_assumed": 0.5,
                  "bfe_month_definitive": 1.0}).to_csv(folder / "ch_demand_option_a_hourly.csv", index=False,
                                                       float_format="%.3f")
    pd.DataFrame({"time_utc": hours, "solar": [100.0 + k for k in steps], "wind_onshore": [10.0 + k for k in steps],
                  "hydro_run_of_river": [1000.0 + k for k in steps], "load": 7000.0}).to_csv(
        folder / "ch_public_power_hourly.csv", index=False, float_format="%.3f")
    return folder
