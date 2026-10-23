"""Small processed folders for back-cast tests: the tables of data_prep/historical, for one short period plus one
hour on each side, in the pipeline's format (index column time_utc, UTC interval start)."""
import pandas as pd

from data_prep.historical_import import BORDER_NEIGHBOURS, GENERATION_TABLES, historical_time_map
from scenarios.settings_default import seetings_default_param

# Asymmetric transfer limits per neighbour (MW), so that a reversed direction shows
INTO_CH = {"AT00": 1200.0, "DE00": 1150.0, "FR00": 2015.0, "IT00": 1910.0}
OUT_OF_CH = {"AT00": 630.0, "DE00": 4000.0, "FR00": 900.0, "IT00": 3716.0}
# Generation table a back-cast reads when its scenario does not set historical_generation_source
GENERATION_TABLE = GENERATION_TABLES[seetings_default_param["historical_generation_source"]][0]


def period_hours(period):
    hours = historical_time_map(period)["time_utc"]
    return pd.DatetimeIndex([hours.iloc[0] - pd.Timedelta(hours=1), *hours, hours.iloc[-1] + pd.Timedelta(hours=1)])


def write_processed(folder, period):
    """Write the processed tables a back-cast reads. Prices rise by 1 EUR/MWh per hour from 50 (DE00 from -5);
    demand_MW rises by 1 MW per hour from 6000, with the solar-shaped column 100 MW below and the flat one 100 MW
    above; every month is published definitively. In the default generation table, solar, wind and run-of-river
    rise by 1 MW per hour from 100, 10 and 1000, nuclear from 2000 and others from 300 (these two restart every
    500 hours). Installed power: nuclear
    2973 MW (net capacity), others 1300 MW in 2024 and 1200 MW in 2025, the latest published year. PECD weeks
    (Monday 00:00 UTC) from the week before the period to the week after it: HRI 168 * (10 + i) MWh in week i,
    HOL 336 MWh. BFE readings on the local Mondays from the week before the period to the week after it:
    TotalCH 6000 + 70 * i GWh in week i, maximum 8895 GWh."""
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
    nuclear = [2000.0 + k % 500 for k in steps]
    pd.DataFrame({"time_utc": hours, "solar": [100.0 + k for k in steps], "wind_onshore": [10.0 + k for k in steps],
                  "hydro_run_of_river": [1000.0 + k for k in steps], "nuclear": nuclear,
                  "others": [300.0 + k % 500 for k in steps], "load": 7000.0}).to_csv(
        folder / GENERATION_TABLE, index=False, float_format="%.3f")
    pd.DataFrame({"time_utc": hours, "generation_MW": nuclear, "net_capacity_MW": 2973.0,
                  "outage_MW": [2973.0 - n for n in nuclear]}).to_csv(
        folder / "nuclear_availability_hourly.csv", index=False, float_format="%.3f")
    pd.DataFrame({"year": [2024, 2025], "nuclear": 2973.0, "others": [1300.0, 1200.0]}).to_csv(
        folder / "ch_installed_power_yearly.csv", index=False, float_format="%.3f")
    pecd_weeks(hours).to_csv(folder / "pecd_inflow_weekly_ch00.csv", index=False, float_format="%.3f")
    bfe_readings(hours).to_csv(folder / "reservoir_weekly_bfe.csv", index=False)
    return folder


def bfe_readings(hours):
    """BFE weekly national stocks in the pipeline's format: one row per local Monday (the reading for Sunday 24:00
    local), from the Monday before the week of the first hour to the Monday after the last hour."""
    local = hours.tz_convert("Europe/Zurich")
    first = (local[0] - pd.Timedelta(days=local[0].dayofweek)).normalize().tz_localize(None) - pd.Timedelta(days=7)
    dates = pd.date_range(first, local[-1].tz_localize(None) + pd.Timedelta(days=8), freq="7D")
    regions = {"Wallis": (3000.0, 10, 4070.0), "Graubuenden": (1500.0, 20, 1980.0), "Tessin": (900.0, 5, 1245.0),
               "UebrigCH": (1000.0, 30, 1600.0)}  # start, rise per week, capacity: each region fills differently
    table = pd.DataFrame({"date": dates.strftime("%Y-%m-%d"),
                          "TotalCH_GWh": [6000.0 + 70 * i for i in range(len(dates))], "TotalCH_max_GWh": 8895.0})
    for region, (first_gwh, rise, capacity) in regions.items():
        table[f"{region}_GWh"] = [first_gwh + rise * i for i in range(len(dates))]
        table[f"{region}_max_GWh"] = capacity
    return table


def pecd_weeks(hours):
    """Weekly PECD rows in the pipeline's format, from the Monday week before the first hour to the week after the
    last hour."""
    first = (hours[0] - pd.Timedelta(days=hours[0].dayofweek)).normalize() - pd.Timedelta(days=7)
    starts = pd.date_range(first, hours[-1] + pd.Timedelta(days=7), freq="7D")
    rows = []
    for series in ("HRI", "HOL"):
        for i, start in enumerate(starts):
            rows.append({"series": series, "week_end_utc": start + pd.Timedelta(hours=168), "week_start_utc": start,
                         "energy_MWh": 168.0 * (10 + i) if series == "HRI" else 336.0, "release": "COM"})
    return pd.DataFrame(rows)
