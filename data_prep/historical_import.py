"""Back-casts: CH_only optimisations of a past period, driven by inputs observed or reconstructed for that
period (glossary: CONTEXT.md; design: CH_ONLY_HISTORICAL_MODEL_PLAN.md; guide: docs/backcast.md).

This module holds the back-cast's time base and setting checks, and the historical import that replaces the
normal data import. It reads processed files only and never imports pipeline code (data_prep/historical).
"""
import csv
import hashlib
import io
import json
import math
import re
import unicodedata
import subprocess
from pathlib import Path

import pandas as pd
from pytz.exceptions import InvalidTimeError

LOCAL_TZ = "Europe/Zurich"
HOURS_PER_YEAR = 8760
PERIOD_FORMAT = re.compile(r"\d{4}-\d{2}-\d{2}/\d{4}-\d{2}-\d{2}")
# Settings that a back-cast replaces with observed inputs; they must stay off (False or 0), so that nobody
# believes they act
SETTINGS_OFF = (
    "hydro_inflow_TWh",
    "ror_annual_TWh",
    "pump_capacity_GW",
    "adding_hydro_storage_cap_TWh",
    "reduce_inflex_demand_by_[MWh]",
    "slack_soc",
    "minimum_RES_target_CH",
)

NODE = "CH00"
CONSUMER = "CH00_fixedconsumer"
# Border lines HVAC_<neighbour>_CH00 with the neighbour as start node, as in the normal CH_only import
BORDER_NEIGHBOURS = ("AT00", "DE00", "FR00", "IT00")
FLEET_TABLES = (
    "input/hydro_PECD/PECD_EERA2021_reservoir_pumping_2030_table.csv",
    "input/hydro_PECD/PECD_EERA2021_reservoir_pumping_2030_table_representative_plants.csv",
)
REPRESENTATIVE_TABLE = FLEET_TABLES[1]  # the open plants of the fleet, with their inflow_share
# Experiment: the WASTA 2025/26 hydro plants (input/hydro_WATSA, copied from Future_Markets, branch price_setters)
HYDRO_FLEETS = ("representative_2030", "wasta_individual")  # setting historical_hydro_fleet
WASTA_PLANTS = "input/hydro_WATSA/hydro_plants_individual.csv"
WASTA_INFLOW = "input/hydro_WATSA/inflow_profiles/plant_donor_map.csv"  # natural inflow per plant
# BFE monthly electricity balance (opendata.swiss ogd35), downloaded 2026-10-12; setting historical_inflow_level
BFE_BALANCE = "input/bfe_ogd35/ogd35_schweizerische_elektrizitaetsbilanz_monatswerte.csv"
HYDRO_TECHS = ("dam", "psp_open", "psp_close")  # the storage hydro of a back-cast
HYDRO_OUTAGES = "input/historical/hydro_outages_2025_26.csv"  # setting historical_hydro_outages
OUTAGE_COLUMNS = ["plant", "turbine_MW", "pump_MW", "source"]  # and one pair of OUTAGE_TIME_COLUMNS
OUTAGE_TIME_COLUMNS = {"local": ("start_local", "end_local"), "utc": ("start_utc", "end_utc")}
OUTAGE_TIME_FORMAT = "%Y-%m-%d %H:%M"
# FOEN daily discharge at donor gauges (experiments/backcast/foen_annual_tables.py, foen_donors.py), downloaded
# 2026-10-12; setting historical_inflow_split
FOEN_DAILY = "input/hydro_FOEN/foen_daily_discharge.csv"
FOEN_DONORS = "input/hydro_FOEN/plant_donors.csv"
GAUGE_MIN_DAYS = 5  # days of discharge a donor gauge needs in every PECD week of the period
AVAILABILITY_TABLE = "input/availability_factor_conventional.csv"
COST_WORKBOOK = "input/cost_assumptions.xlsx"
WORKBOOK_YEARS = (2035, 2050)
# Inputs the historical import does not build yet; the step that builds one removes its entry
NOT_YET_BUILT = []
RUN_RECORDS = {}  # sub-scenario -> time map and inputs manifest, written by core_main into the run folder
# Tables of the processed folder of data_prep/historical (historical_data_dir)
PRICES_TABLE = "prices_day_ahead_hourly.csv"
NTC_TABLE = "ntc_dayahead_combined_hourly.csv"
# Setting historical_border_flows: "ntc" = each border line's flow between its transfer limits; "scheduled" = fixed
# to the observed scheduled exchange of EXCHANGE_TABLE (+ import), a diagnostic
BORDER_FLOW_MODES = ("ntc", "scheduled")
EXCHANGE_TABLE = "ch_exchanges_scheduled_hourly.csv"
DEMAND_TABLE = "ch_demand_option_a_hourly.csv"
# Demand columns of DEMAND_TABLE (setting historical_demand) and where each places on-site consumption
DEMAND_PLACEMENT = {
    "demand_MW": "a share {share} (onsite_solar_share_assumed) like PV output, the rest flat",
    "demand_solar_shaped_MW": "all like PV output",
    "demand_flat_MW": "all flat",
}
DEMAND_TYPE = "fixed"  # the only inflexible consumption type of the model (core_main)
# Observed Swiss generation (setting historical_generation_source) -> processed table and what it holds
GENERATION_TABLES = {
    "energy_charts": ("ch_public_power_hourly.csv", "Energy-Charts public power"),
    "entsoe": ("ch_generation_entsoe_hourly.csv", "ENTSO-E A75 as published, which covers only part of Swiss "
               "generation; 'others' from BFE, as ENTSO-E has none for CH"),
    "entsoe_bfe_months": ("ch_generation_entsoe_bfe_hourly.csv", "ENTSO-E A75 hourly profiles, each local month "
                          "scaled to BFE's production of the type; 'others' from BFE, as ENTSO-E has none for CH"),
}
# Infeed technology of the model -> observed column of the generation table; every other infeed technology is 0
INFEED_COLUMNS = {"pvrf": "solar", "windon": "wind_onshore", "ror": "hydro_run_of_river"}
NUCLEAR_TABLE = "nuclear_availability_hourly.csv"
INSTALLED_TABLE = "ch_installed_power_yearly.csv"
# Fixed plants, whose output equals an observed column of the generation table: plant -> technology and column
FIXED_PLANT_TECH = {"CH03_nuclear": "nuclear", "CH01_other": "other"}
FIXED_PLANT_SERIES = {"CH03_nuclear": "nuclear", "CH01_other": "others"}
PECD_TABLE = "pecd_inflow_weekly_ch00.csv"
# PECD series of CH00 whose sum is the natural inflow of the open plants: reservoir (HRI) and open-loop pumped
# storage (HOL)
PECD_SERIES = ("HRI", "HOL")
WEEK_HOURS = 168
RESERVOIR_TABLE = "reservoir_weekly_bfe.csv"
NATIONAL_GROUP = "TotalCH"  # reservoir group of every open plant without historical_hydro_regions; a BFE column prefix
BFE_REGIONS = {"VS": "Wallis", "GR": "Graubuenden", "TI": "Tessin"}  # canton -> BFE region; any other canton:
OTHER_REGION = "UebrigCH"
HYDRO_END_MODES = ("equal", "at_least", "free")  # setting historical_hydro_end
MAX_READING_GAP = pd.Timedelta(days=8)  # longest gap between the two readings an instant is interpolated from
# Setting historical_hydro_fixed_units: a CSV of plant groups (one or more WASTA plants and the ENTSO-E A73 units
# that report them, ";"-separated); a group with fix = yes produces its units' observed output (UNITS_TABLE)
UNITS_TABLE = "ch_generation_units_entsoe_hourly.csv"
UNIT_GROUP_COLUMNS = ["group", "entsoe_units", "plants", "fix", "note"]
UNIT_ROUNDING_MW = 1.0  # published unit output may exceed the installed capacity by this much (rounding); more raises


def is_backcast(settings):
    return settings["historical_period"] is not False


def period_bounds_utc(historical_period):
    """UTC interval [start, end) of a historical period "YYYY-MM-DD/YYYY-MM-DD" of local dates, both included."""
    if not isinstance(historical_period, str) or not PERIOD_FORMAT.fullmatch(historical_period):
        raise ValueError(
            f"historical_period must be a quoted string 'YYYY-MM-DD/YYYY-MM-DD', got {historical_period!r}; the "
            f"scenario reader turns an unquoted date range into a number and an empty cell into 'nan'"
        )
    try:
        first, last = (pd.Timestamp(day) for day in historical_period.split("/"))
    except ValueError as error:
        raise ValueError(f"historical_period {historical_period!r} names an invalid date: {error}") from error
    if last < first:
        raise ValueError(f"historical_period {historical_period!r} ends before it starts")
    if any((day.month, day.day) == (2, 29) for day in pd.date_range(first, last, freq="D")):
        raise ValueError(f"historical_period {historical_period!r} contains 29 February, which the model's "
                         f"365-day time steps cannot hold yet")
    start = first.tz_localize(LOCAL_TZ).tz_convert("UTC")
    end = (last + pd.Timedelta(days=1)).tz_localize(LOCAL_TZ).tz_convert("UTC")
    hours = int((end - start) / pd.Timedelta(hours=1))
    if hours > HOURS_PER_YEAR:
        raise ValueError(f"historical_period {historical_period!r} has {hours} hours, more than {HOURS_PER_YEAR}")
    return start, end


def historical_time_map(historical_period):
    """Model step, UTC hour and local hour of every hour of the period, in order.

    The k-th UTC hour becomes model step t_((offset + k - 1) mod 8760) + 1, where offset is the hour of year of
    the first local hour in a 365-day reference calendar, so 1 October 00:00 local is t_6553 in any year. The
    labels are positions, not calendar hours: dates come from this map, never from the labels.
    """
    start, end = period_bounds_utc(historical_period)
    first_day = pd.Timestamp(historical_period.split("/")[0])
    offset = (pd.Timestamp(2001, first_day.month, first_day.day).dayofyear - 1) * 24  # 2001 has 365 days
    hours_utc = pd.date_range(start, end, freq="h", inclusive="left")
    steps = [f"t_{(offset + k) % HOURS_PER_YEAR + 1}" for k in range(len(hours_utc))]
    return pd.DataFrame({"t": steps, "time_utc": hours_utc, "time_local": hours_utc.tz_convert(LOCAL_TZ)})


def model_steps(historical_period):
    """(t_start, t_end): the first and last model step of the period."""
    steps = historical_time_map(historical_period)["t"]
    return int(steps.iloc[0][2:]), int(steps.iloc[-1][2:])


def check_backcast_settings(scenario_name, settings):
    """Raise, naming every conflict, unless the settings of a back-cast sub-scenario are consistent."""
    problems = []
    if settings["CH_only"] is not True:
        problems.append(f"CH_only must be True, got {settings['CH_only']!r}")
    if settings["allow_investment"] is not False:
        problems.append(f"allow_investment must be False, got {settings['allow_investment']!r}")
    if settings["rep_hydro_plants"] is not True:
        problems.append(f"rep_hydro_plants must be True (the representative hydro fleet), "
                        f"got {settings['rep_hydro_plants']!r}")
    swiss_nodes = [node for node in settings["Node_list_setting"] if node.startswith("CH")]
    if swiss_nodes != ["CH00"]:
        problems.append(f"the Swiss nodes of Node_list_setting must be exactly ['CH00'], got {swiss_nodes}")
    for name in SETTINGS_OFF:
        if settings[name] not in (False, 0):
            problems.append(f"{name} must be False or 0, got {settings[name]!r}")
    winter_limit = settings["winter_limit"]
    if not isinstance(winter_limit, dict) or winter_limit.get("mode") is not False:
        problems.append(f"winter_limit must have mode False, got {winter_limit!r}")
    if settings["NodeDH_list"] != []:
        problems.append(f"NodeDH_list must be [], got {settings['NodeDH_list']!r}")
    if not isinstance(settings["historical_data_dir"], str) or not settings["historical_data_dir"]:
        problems.append(f"historical_data_dir must name the processed folder, got {settings['historical_data_dir']!r}")
    if problems:
        raise ValueError(f"back-cast settings of {scenario_name}: " + "; ".join(problems))


def check_backcast_meta_scenario(scenario_name, settings_by_sub_scenario):
    """A back-cast meta-scenario has exactly one sub-scenario (weight 1)."""
    if any(is_backcast(settings) for settings in settings_by_sub_scenario.values()) and len(settings_by_sub_scenario) != 1:
        raise ValueError(f"back-cast meta-scenario {scenario_name} must have exactly one sub-scenario, "
                         f"got {list(settings_by_sub_scenario)}")


def import_historical_inputs(scenario_name, settings):
    """Fill the model's global lists and dicts for a back-cast instead of the normal import.

    Every input is read and checked first; only then are the globals written, so a failing input changes
    nothing. The globals must be empty when it starts (check_no_foreign_state).
    """
    check_no_foreign_state(scenario_name)
    staged, files, assumptions = read_historical_inputs(scenario_name, settings)
    if NOT_YET_BUILT:
        raise NotImplementedError(f"{scenario_name}: back-cast inputs not built yet: {', '.join(NOT_YET_BUILT)}")
    write_staged(staged)
    RUN_RECORDS[scenario_name] = {
        "time_map": historical_time_map(settings["historical_period"]),
        "manifest": inputs_manifest(scenario_name, settings, files, assumptions),
    }


def read_historical_inputs(scenario_name, settings):
    """All inputs of one back-cast sub-scenario, checked but not written: (global name -> entries, files read,
    assumptions)."""
    from model.data_import_fcns import read_lost_load_cost

    staged = {}
    stage(staged, read_structure(scenario_name))
    stage(staged, {"lost_load_cost": {scenario_name: read_lost_load_cost(settings["lost_load_cost_mode"])}})
    fleet, fleet_assumptions = read_hydro_fleet(scenario_name, settings)
    stage(staged, fleet)
    market, market_files, market_assumptions = read_market(scenario_name, settings)
    stage(staged, market)
    demand, demand_files, demand_assumptions = read_demand(scenario_name, settings)
    stage(staged, demand)
    infeed, infeed_files, infeed_assumptions = read_infeed(scenario_name, settings)
    stage(staged, infeed)
    fixed, fixed_files, fixed_assumptions = read_fixed_plants(scenario_name, settings)
    stage(staged, fixed)
    stage(staged, {"Map_node_plant": {NODE: list(staged["Plant_list"])}})
    inflow, inflow_files, inflow_assumptions = read_inflow(scenario_name, settings, fleet["Plant_list"],
                                                           pump_round_trip(fleet, scenario_name))
    stage(staged, inflow)
    stocks, stock_files, stock_assumptions = read_reservoir_stocks(scenario_name, settings, fleet)
    stage(staged, stocks)
    units, unit_files, unit_assumptions = read_fixed_units(scenario_name, settings, fleet, inflow, stocks)
    stage(staged, units)
    fixed_plants = [p for plants in units["Hydro_fixed_group"].values() for p in plants]
    operation, operation_files, operation_assumptions = read_hydro_operation(scenario_name, settings, fleet,
                                                                             fixed_plants)
    stage(staged, operation)
    files = [*fleet_files(settings), AVAILABILITY_TABLE, COST_WORKBOOK,
             f"input/LostLoadCost/lostLoadCost_{settings['lost_load_cost_mode']}.csv", *market_files, *demand_files,
             *infeed_files, *fixed_files, *inflow_files, *stock_files, *unit_files, *operation_files]
    files = list(dict.fromkeys(files))  # a table two inputs read is recorded once
    assumptions = [
        f"border lines HVAC_<neighbour>_{NODE} for {', '.join(BORDER_NEIGHBOURS)}, the neighbour as start node",
        *fleet_assumptions,
        "investment is off: every investment slope is 0; operating-cost calibration 1; no limited fuels",
        *market_assumptions,
        *demand_assumptions,
        *infeed_assumptions,
        *fixed_assumptions,
        *inflow_assumptions,
        *stock_assumptions,
        *unit_assumptions,
        *operation_assumptions,
    ]
    return staged, files, assumptions


def read_market(scenario_name, settings):
    """Neighbour prices and day-ahead transfer limits on the four border lines, with the files read and the
    assumptions. For line HVAC_X00_CH00 the upper flow bound (ATC_exportlimit) is X_to_CH and the lower bound
    is minus CH_to_X (ATC_importlimit); NTC_CH_ratio multiplies both directions once. With historical_border_flows
    = 'scheduled' both bounds are the observed scheduled exchange, which fixes the flow."""
    ratio = settings["NTC_CH_ratio"]
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not math.isfinite(ratio) or ratio < 0:
        raise ValueError(f"NTC_CH_ratio must be a finite number of at least 0, got {ratio!r}")
    prices = read_processed_hourly(settings, PRICES_TABLE, list(BORDER_NEIGHBOURS))
    into_ch = {n: f"{n[:2]}_to_CH" for n in BORDER_NEIGHBOURS}
    out_of_ch = {n: f"CH_to_{n[:2]}" for n in BORDER_NEIGHBOURS}
    limits = read_processed_hourly(settings, NTC_TABLE, [*into_ch.values(), *out_of_ch.values()])
    negative = [column for column in limits.columns if (limits[column] < 0).any()]
    if negative:
        raise ValueError(f"{NTC_TABLE}: negative transfer limits in {negative}")
    s = scenario_name
    lines = {n: f"HVAC_{n}_{NODE}" for n in BORDER_NEIGHBOURS}
    staged = {
        "Line_trade_price": {(lines[n], t, s): float(prices.at[t, n]) for n in BORDER_NEIGHBOURS for t in prices.index},
        "ATC_exportlimit": {(lines[n], t, s): ratio * float(limits.at[t, into_ch[n]])
                            for n in BORDER_NEIGHBOURS for t in limits.index},
        "ATC_importlimit": {(lines[n], t, s): ratio * float(limits.at[t, out_of_ch[n]])
                            for n in BORDER_NEIGHBOURS for t in limits.index},
    }
    ntc_path = Path(settings["historical_data_dir"]) / NTC_TABLE
    source_columns = [f"source_{direction}" for direction in [*into_ch.values(), *out_of_ch.values()]]
    sources = pd.read_csv(ntc_path, usecols=["time_utc", *source_columns])
    sources.index = pd.to_datetime(sources["time_utc"], utc=True)
    in_period = sources.loc[historical_time_map(settings["historical_period"])["time_utc"], source_columns]
    counts = pd.Series(in_period.to_numpy().ravel()).value_counts().to_dict()
    assumptions = [
        "neighbour prices: day-ahead, EUR/MWh, hourly means of the quarter-hours; negative prices kept",
        f"transfer limits: combined day-ahead NTC (Swissgrid D-2, else ENTSO-E), hourly minimum of the "
        f"quarter-hours, times NTC_CH_ratio = {ratio}; direction-hours per source: {counts}",
    ]
    files = [str(Path(settings["historical_data_dir"]) / PRICES_TABLE), str(ntc_path)]
    mode = settings["historical_border_flows"]
    if mode not in BORDER_FLOW_MODES:
        raise ValueError(f"historical_border_flows must be one of {list(BORDER_FLOW_MODES)}, got {mode!r}")
    if mode == "scheduled":
        observed = read_processed_hourly(settings, EXCHANGE_TABLE, list(BORDER_NEIGHBOURS))
        staged["ATC_exportlimit"] = {(lines[n], t, s): float(observed.at[t, n])
                                     for n in BORDER_NEIGHBOURS for t in observed.index}
        staged["ATC_importlimit"] = {(lines[n], t, s): -float(observed.at[t, n])
                                     for n in BORDER_NEIGHBOURS for t in observed.index}
        above = {n: int(((observed[n] > ratio * limits[into_ch[n]]) | (-observed[n] > ratio * limits[out_of_ch[n]]))
                        .sum()) for n in BORDER_NEIGHBOURS}
        assumptions[1] = (f"border flows fixed to the observed scheduled exchange of {EXCHANGE_TABLE} (+ import into "
                          f"CH), historical_border_flows = 'scheduled'; the transfer limits above are not applied; "
                          f"hours the observed exchange lies outside them, per neighbour: {above}")
        files.append(str(Path(settings["historical_data_dir"]) / EXCHANGE_TABLE))
    return staged, files, assumptions


def read_demand(scenario_name, settings):
    """Swiss demand option A, column historical_demand, as the fixed consumer's demand at every model step, with
    the file read and the assumptions. EV and heat-pump inflexible demand stay empty: observed demand holds them."""
    column = settings["historical_demand"]
    if column not in DEMAND_PLACEMENT:
        raise ValueError(f"historical_demand must be one of {list(DEMAND_PLACEMENT)}, got {column!r}")
    table = read_processed_hourly(settings, DEMAND_TABLE,
                                  [column, "onsite_solar_share_assumed", "bfe_month_definitive"])
    negative = table.index[table[column] < 0]
    if len(negative):
        raise ValueError(f"{DEMAND_TABLE}: {len(negative)} negative values of {column}, e.g. at {negative[0]}")
    shares = sorted(set(table["onsite_solar_share_assumed"]))
    if len(shares) != 1:
        raise ValueError(f"{DEMAND_TABLE}: onsite_solar_share_assumed varies within the period ({shares})")
    provisional = int((table["bfe_month_definitive"] == 0).sum())
    s = scenario_name
    staged = {"Demand_data": {(CONSUMER, DEMAND_TYPE, t, s): float(table.at[t, column]) for t in table.index}}
    assumptions = [
        f"Swiss demand: option A, column {column}: Swissgrid end-user consumption + grid losses + on-site "
        f"consumption, without storage pumping; on-site consumption placed "
        f"{DEMAND_PLACEMENT[column].format(share=shares[0])}",
        f"monthly demand equals BFE Landesverbrauch; {provisional} of {len(table)} hours lie in months BFE has "
        f"published only provisionally",
        "EV and heat-pump inflexible demand: none, as observed demand already holds them",
    ]
    return staged, [str(Path(settings["historical_data_dir"]) / DEMAND_TABLE)], assumptions


def generation_table(settings):
    """Processed table of the observed Swiss generation and its description, by historical_generation_source."""
    source = settings["historical_generation_source"]
    if source not in GENERATION_TABLES:
        raise ValueError(f"historical_generation_source must be one of {list(GENERATION_TABLES)}, got {source!r}")
    return GENERATION_TABLES[source]


def read_infeed(scenario_name, settings):
    """Observed solar, wind and run-of-river generation as the fixed consumer's infeed at every model step, and 0
    for every other infeed technology, with the file read and the assumptions. Curtailment stays possible."""
    from model.structural_parameters import tech_infeed_all_list

    power_table, described = generation_table(settings)
    table = read_processed_hourly(settings, power_table, list(INFEED_COLUMNS.values()))
    negative = [column for column in table.columns if (table[column] < 0).any()]
    if negative:
        raise ValueError(f"{power_table}: negative generation in {negative}")
    s = scenario_name
    infeed = {(CONSUMER, tech, t, s): float(table.at[t, column]) for tech, column in INFEED_COLUMNS.items()
              for t in table.index}
    others = [tech for tech in tech_infeed_all_list if tech not in INFEED_COLUMNS]
    infeed.update({(CONSUMER, tech, t, s): 0.0 for tech in others for t in table.index})
    energy = ", ".join(f"{tech} {table[column].sum() / 1e6:.3f} TWh" for tech, column in INFEED_COLUMNS.items())
    assumptions = [
        f"renewable infeed: {described} ({power_table}), "
        f"{', '.join(f'{column} as {tech}' for tech, column in INFEED_COLUMNS.items())}; {energy} over the period",
        f"infeed of {', '.join(others)}: 0; no infeed plants, so solar, wind and run-of-river capacities are not "
        f"modelled",
    ]
    return {"Infeed_consumers": infeed}, [str(Path(settings["historical_data_dir"]) / power_table)], assumptions


def read_fixed_plants(scenario_name, settings):
    """Nuclear and "others" as fixed plants, whose output equals the observed series, with the files read and the
    assumptions. The model holds one capacity per plant, so gen_max is P_base, the maximum over the period of the
    capacity C(t) of each hour, and availability is observed / P_base; C(t) only validates."""
    power_table, described = generation_table(settings)
    observed = read_processed_hourly(settings, power_table, list(FIXED_PLANT_SERIES.values()))
    observed = observed.rename(columns={column: p for p, column in FIXED_PLANT_SERIES.items()})
    others, years = installed_capacity(settings, "others")
    capacity = pd.DataFrame({
        "CH03_nuclear": read_processed_hourly(settings, NUCLEAR_TABLE, ["net_capacity_MW"])["net_capacity_MW"],
        "CH01_other": others,
    })
    problems = []
    for p in FIXED_PLANT_TECH:
        negative = observed.index[observed[p] < 0]
        above = observed.index[observed[p] > capacity[p]]
        if len(negative):
            problems.append(f"{p}: {len(negative)} negative observed values, e.g. at {negative[0]}")
        if len(above):
            problems.append(f"{p}: {len(above)} observed values above the capacity C(t), e.g. at {above[0]} "
                            f"({observed.at[above[0], p]} > {capacity.at[above[0], p]} MW)")
        if not capacity[p].max() > 0:
            problems.append(f"{p}: the normalisation capacity P_base is {capacity[p].max()}, not positive")
    if problems:
        raise ValueError("fixed plants: " + "; ".join(problems))
    p_base = capacity.max()
    techs = sorted(set(FIXED_PLANT_TECH.values()))
    eff_out = year_independent("efficiency", techs)
    emission = year_independent("emission_factor", techs)
    s = scenario_name
    plants = list(FIXED_PLANT_TECH)
    staged = {
        "Plant_list": plants,
        "Map_plant_tech": dict(FIXED_PLANT_TECH),
        "Map_plant_node": {p: NODE for p in plants},
        "Plant_capacity_gen": {(p, s): float(p_base[p]) for p in plants},
        "Avail_plant": {(p, t, s): float(observed.at[t, p] / p_base[p]) for p in plants for t in observed.index},
        # output is fixed, so an operating cost would only add a constant to the objective
        "cost_data_opr_slp": {(tech, s): 0.0 for tech in techs},
        "cost_data_opr_int": {(tech, s): 0.0 for tech in techs},
        "op_cost_n_tech_calibration": {(NODE, tech, s): 1.0 for tech in techs},
        "emission_factor_per_MWh": {(tech, s): emission[tech] / eff_out[tech] for tech in techs},
        "cost_data_inv_gen_slp": {(tech, s): 0.0 for tech in techs},
        "cost_data_inv_e_slp": {(tech, s): 0.0 for tech in techs},
        "cost_data_inv_discharge_slp": {(tech, s): 0.0 for tech in techs},
    }
    assumptions = [
        f"fixed plants: output equals the observed series of {described} ({power_table}), "
        f"{', '.join(f'{p} = {column}' for p, column in FIXED_PLANT_SERIES.items())}; gen_max is P_base, the "
        f"maximum of the capacity C(t) over the period, and availability is observed / P_base",
        f"C(t) of CH03_nuclear: net_capacity_MW of {NUCLEAR_TABLE}; P_base {p_base['CH03_nuclear']} MW",
        f"C(t) of CH01_other: 'others' of {INSTALLED_TABLE} for the latest published year not after the local year "
        f"of each hour (local year -> published year {years}); P_base {p_base['CH01_other']} MW",
        "'others' is the BFE monthly thermal total spread flat over each month, not an hourly measurement",
        f"operating cost of {', '.join(techs)}: 0, as their output is fixed; emission factors {emission} tCO2/MWh "
        f"of input from the cost workbook",
    ]
    folder = Path(settings["historical_data_dir"])
    files = [str(folder / name) for name in (power_table, NUCLEAR_TABLE, INSTALLED_TABLE)]
    return staged, files, assumptions


def read_inflow(scenario_name, settings, hydro_plants, round_trip):
    """Natural inflow of the open plants at every model step, PECD HRI + HOL split by inflow_share, and 0 for the
    other plants of the hydro fleet; with the files read and the assumptions. Inflow is electrical energy at grid
    level (C3S user guide), which the electrical basis of read_hydro_fleet matches. With historical_inflow_level =
    'bfe_mass_balance', each local month is rescaled to the BFE mass balance (mass_balance_level), using the open
    plants' pump-cycle round trip. With historical_inflow_split = 'foen_gauges', each week's national inflow is
    split by the plants' donor gauges (gauge_split) instead of fixed shares."""
    level = settings["historical_inflow_level"]
    if level not in ("pecd", "bfe_mass_balance"):
        raise ValueError(f"historical_inflow_level must be 'pecd' or 'bfe_mass_balance', got {level!r}")
    split_mode = settings["historical_inflow_split"]
    if split_mode not in ("fixed_shares", "foen_gauges"):
        raise ValueError(f"historical_inflow_split must be 'fixed_shares' or 'foen_gauges', got {split_mode!r}")
    if split_mode == "foen_gauges" and settings["historical_hydro_fleet"] != "wasta_individual":
        raise ValueError(f"historical_inflow_split = 'foen_gauges' needs historical_hydro_fleet = 'wasta_individual' "
                         f"(donor gauges of WASTA plants), got {settings['historical_hydro_fleet']!r}")
    path = processed_table(settings, PECD_TABLE, ["series", "week_start_utc", "week_end_utc", "energy_MWh", "release"])
    table = pd.read_csv(path, usecols=["series", "week_start_utc", "week_end_utc", "energy_MWh", "release"])
    time_map = historical_time_map(settings["historical_period"])
    hourly, overlap, releases = {}, {}, {}
    for series in PECD_SERIES:
        hourly[series], overlap[series], releases[series] = spread_weeks(table[table["series"] == series], time_map,
                                                                       series)
    total = sum(hourly.values())
    level_files, level_assumptions = [], []
    if level == "bfe_mass_balance":
        total, level_files, level_assumptions = mass_balance_level(settings, time_map, total, round_trip)
    shares = fleet_inflow_shares(settings)
    s = scenario_name
    split_files, split_assumptions = [], []
    if split_mode == "fixed_shares":
        inflow = {(p, t, s): float(share * total[t]) for p, share in shares.items() for t in total.index}
    else:
        split, week_of, split_files, split_assumptions = gauge_split(time_map, shares)
        inflow = {(p, t, s): float(split.at[p, week_of[t]] * total[t]) for p in shares for t in total.index}
    others = [p for p in hydro_plants if p not in shares]
    inflow.update({(p, t, s): 0.0 for p in others for t in total.index})
    energy = ", ".join(f"{series} {hourly[series].sum() / 1e6:.3f} TWh" for series in PECD_SERIES)
    assumptions = [
        f"reservoir inflow: PECD 4.2 HRI + HOL of CH00 ({PECD_TABLE}), electrical energy at grid level; {energy} "
        f"over the period; weeks by release {releases}",
        f"each PECD week is 168 UTC hours from Monday 00:00 UTC (inferred from the files, not stated by the "
        f"provider); its energy is spread flat and clipped to the period, nothing renormalised and no week "
        f"interpolated; hours of the first and last week in the period, of {WEEK_HOURS}: {overlap}",
        f"inflow split by inflow_share of {fleet_files(settings)[-1]}: {shares}; inflow of {', '.join(others)}: 0",
        *level_assumptions,
        *split_assumptions,
    ]
    staged = {"Inflow_data": inflow, "Plant_inflow_list": [*shares, *others]}
    return staged, [str(path), fleet_files(settings)[-1], *level_files, *split_files], assumptions


def gauge_split(time_map, shares):
    """Each open plant's share of every PECD week's national inflow from its FOEN donor gauge (gauge_weekly_split),
    the PECD week (its Monday) of every model step, the files read and the assumptions."""
    donors = pd.read_csv(FOEN_DONORS, encoding="utf-8-sig")
    donor_of = {wasta_plant(c, n): int(d) for c, n, d in zip(donors["category"], donors["name"], donors["donor"])}
    daily = pd.read_csv(FOEN_DAILY, parse_dates=["date"])
    utc = pd.DatetimeIndex(time_map["time_utc"])
    monday = (utc.normalize() - pd.to_timedelta(utc.dayofweek, unit="D")).tz_localize(None)
    week_of = dict(zip(time_map["t"], monday))
    split = gauge_weekly_split(shares, donor_of, daily, sorted(set(monday)))
    replaced = int((donors["donor"] != donors["wasta_donor"]).sum()) if "wasta_donor" in donors else 0
    assumptions = [
        f"inflow split between plants by FOEN donor gauges ({FOEN_DONORS}, {FOEN_DAILY}): each week a plant's share "
        f"is its natural-inflow share times its gauge's mean discharge that week relative to the gauge's mean over "
        f"the period's weeks, renormalised so the national weekly inflow is unchanged; this keeps each region's "
        f"timing, not its wetness in this year; {len(set(donor_of.values()))} gauges, {replaced} plants on another "
        f"gauge than WASTA's (its gauge lacked data); gauge days are local days, PECD weeks UTC weeks (1 to 2 hours "
        f"apart); a week needs {GAUGE_MIN_DAYS} days of discharge",
    ]
    return split, week_of, [FOEN_DONORS, FOEN_DAILY], assumptions


def gauge_weekly_split(shares, donor_of, daily, weeks):
    """Share of each plant in each week (columns: the weeks' Mondays): share x the donor's mean discharge in the week
    / its mean over the weeks, renormalised per week. Raises for a plant without donor or a donor week with fewer
    than GAUGE_MIN_DAYS days of discharge."""
    missing = [p for p in shares if p not in donor_of]
    if missing:
        raise ValueError(f"no donor gauge for the open plants {missing[:5]}")
    weekly = {}
    for gauge in sorted({donor_of[p] for p in shares}):
        series = daily[daily["station"] == gauge].set_index("date")["discharge_m3s"]
        means = []
        for week in weeks:
            days = series[(series.index >= week) & (series.index < week + pd.Timedelta(days=7))]
            if len(days) < GAUGE_MIN_DAYS:
                raise ValueError(f"gauge {gauge} has {len(days)} days of discharge in the week of {week:%Y-%m-%d}, "
                                 f"fewer than {GAUGE_MIN_DAYS}")
            means.append(days.mean())
        weekly[gauge] = pd.Series(means, index=weeks) / (sum(means) / len(means))
    weights = pd.DataFrame({p: share * weekly[donor_of[p]] for p, share in shares.items()}).T
    return weights / weights.sum()


def pump_round_trip(fleet, scenario_name):
    """Electricity regained per unit pumped, charging x discharging efficiency, the same for every plant with pumps
    (open plants on the electrical basis, closed loops on their workbook efficiencies); raises if they differ."""
    pumps = [p for p in fleet["Plant_list"] if fleet["Plant_capacity_pmp"].get((p, scenario_name), 0.0) > 0]
    trips = {round(fleet["Map_eff_in_plant"][p] * fleet["Map_eff_out_plant"][p], 12) for p in pumps}
    if len(trips) != 1:
        raise ValueError(f"the plants with pumps {pumps} disagree on their round trip: {sorted(trips)}")
    return trips.pop()


def mass_balance_level(settings, time_map, pecd_total, round_trip):
    """PECD's hourly national inflow rescaled, month by month, to the BFE mass balance of that local month: national
    stock change (the weekly readings, linear between them) + storage-plant generation - round_trip x pump
    consumption (BFE monthly balance). The PECD shape within a month is kept. The period must be whole local
    months. Returns the rescaled series, the files read and the assumptions."""
    local = pd.DatetimeIndex(time_map["time_local"])
    if not (local[0] == local[0].normalize() and local[0].day == 1
            and (local[-1] + pd.Timedelta(hours=1)).day == 1 and (local[-1] + pd.Timedelta(hours=1)).hour == 0):
        raise ValueError(f"historical_inflow_level = 'bfe_mass_balance' needs a period of whole local months, got "
                         f"{settings['historical_period']}")
    balance, dropped = bfe_balance()
    stock_path = processed_table(settings, RESERVOIR_TABLE, ["date", "TotalCH_GWh"])
    stocks = pd.read_csv(stock_path, usecols=["date", "TotalCH_GWh"]).dropna()
    stocks = pd.Series(stocks["TotalCH_GWh"].to_numpy(),
                       index=pd.DatetimeIndex(pd.to_datetime(stocks["date"]).dt.tz_localize(LOCAL_TZ)
                                              .dt.tz_convert("UTC"))).sort_index()
    month = pd.Series(local.strftime("%Y-%m"), index=time_map["t"].to_numpy())
    factors, rows = {}, []
    for key in dict.fromkeys(month):
        year, number = int(key[:4]), int(key[5:])
        if (year, number) not in balance.index:
            raise ValueError(f"{BFE_BALANCE}: no BFE balance for {key}")
        first = pd.Timestamp(year, number, 1).tz_localize(LOCAL_TZ).tz_convert("UTC")
        after = (pd.Timestamp(year, number, 1) + pd.DateOffset(months=1)).tz_localize(LOCAL_TZ).tz_convert("UTC")
        change = fill_at(stocks, after)[0] - fill_at(stocks, first)[0]
        generation, pumping, final = balance.loc[(year, number)]
        natural = 1000 * (change + generation - round_trip * pumping)  # MWh
        if natural < 0:
            raise ValueError(f"negative natural inflow in {key} from the BFE mass balance: stock change {change:.1f} "
                             f"+ generation {generation} - {round_trip} x pumping {pumping} GWh")
        steps = month.index[month == key]
        factors[key] = natural / float(pecd_total[steps].sum())
        rows.append(f"{key}: {natural / 1e3:.0f} GWh (PECD {pecd_total[steps].sum() / 1e3:.0f}, factor "
                    f"{factors[key]:.3f}{', provisional' if not final else ''})")
    rescaled = pecd_total * month.reindex(pecd_total.index).map(factors).to_numpy()
    assumptions = [
        f"monthly level: BFE mass balance, natural inflow = national stock change ({RESERVOIR_TABLE}, linear between "
        f"the weekly readings) + storage-plant generation - {round_trip} x pump consumption ({BFE_BALANCE}); PECD's "
        f"shape kept within each local month: " + "; ".join(rows),
        f"{dropped} lines of the BFE file are not data and were left out (a notice of the publisher)",
    ]
    return rescaled, [BFE_BALANCE, str(stock_path)], assumptions


def bfe_balance():
    """Storage-plant generation and pump consumption (GWh) and the final flag per (year, month) of the BFE monthly
    electricity balance, and how many lines of the file are not part of its table."""
    lines, header = bfe_lines()
    data = [line for line in lines[header + 1:] if re.match(r"^\d{4},\d{1,2},", line)]
    table = pd.read_csv(io.StringIO("\n".join([lines[header], *data])), na_values=["NULL"])
    columns = ["Erzeugung_Speicherwerk_GWh", "Verbrauch_Speicherpumpen_GWh", "Definitiv"]
    missing = [c for c in columns if c not in table.columns]
    if missing:
        raise ValueError(f"{BFE_BALANCE} lacks the columns {missing}")
    table = table.set_index(["Jahr", "Monat"])[columns]
    bad = table[table.isna().any(axis=1)]
    if len(bad):
        raise ValueError(f"{BFE_BALANCE}: missing values for {list(bad.index)[:3]}")
    table["Definitiv"] = table["Definitiv"].astype(bool)
    return table, len(lines) - 1 - len(data)


def bfe_lines():
    """The lines of the BFE monthly balance (latin-1; the publisher adds notice lines around the table) and the
    position of its one header line."""
    lines = Path(BFE_BALANCE).read_bytes().decode("latin-1").splitlines()
    header = [i for i, line in enumerate(lines) if line.startswith("Jahr,Monat,")]
    if len(header) != 1:
        raise ValueError(f"{BFE_BALANCE}: expected one header line starting 'Jahr,Monat,', found {len(header)}")
    return lines, header[0]


def spread_weeks(weeks, time_map, series):
    """Hourly inflow of one PECD series at every model step: each week's energy spread flat over its 168 UTC hours.
    Also the hours of the first and last week that lie in the period, and the weeks per release. Raises on a week
    that is not 168 hours, overlapping weeks, an hour no week covers, or a missing, non-finite or negative energy of
    a week in the period."""
    start = pd.to_datetime(weeks["week_start_utc"], utc=True)
    end = pd.to_datetime(weeks["week_end_utc"], utc=True)
    weeks = weeks.assign(start=start, end=end).sort_values("start").reset_index(drop=True)
    wrong = weeks[(weeks["end"] - weeks["start"]) != pd.Timedelta(hours=WEEK_HOURS)]
    if len(wrong):
        raise ValueError(f"{PECD_TABLE}: {series} week starting {wrong['start'].iloc[0]} is not {WEEK_HOURS} hours")
    overlapping = weeks[weeks["start"] < weeks["end"].shift()]
    if len(overlapping):
        raise ValueError(f"{PECD_TABLE}: {series} weeks overlap, e.g. at {overlapping['start'].iloc[0]}")
    hours = pd.DatetimeIndex(time_map["time_utc"])
    position = pd.DatetimeIndex(weeks["start"]).searchsorted(hours, side="right") - 1
    covered = position >= 0
    covered[covered] = hours[covered] < pd.DatetimeIndex(weeks["end"])[position[covered]]
    if not covered.all():
        raise ValueError(f"{PECD_TABLE}: no {series} week covers {int((~covered).sum())} hours of the period, "
                         f"e.g. {hours[~covered][0]}")
    used = weeks.loc[sorted(set(position))]
    bad = used[~used["energy_MWh"].between(0, math.inf, inclusive="left")]
    if len(bad):
        raise ValueError(f"{PECD_TABLE}: {series} weeks with missing, non-finite or negative energy in the period, "
                         f"e.g. the week starting {bad['start'].iloc[0]} ({bad['energy_MWh'].iloc[0]})")
    values = weeks["energy_MWh"].to_numpy()[position] / WEEK_HOURS
    overlap = (int((position == position[0]).sum()), int((position == position[-1]).sum()))
    releases = used["release"].value_counts().to_dict()
    return pd.Series(values, index=time_map["t"].to_numpy()), overlap, releases


def read_reservoir_stocks(scenario_name, settings, fleet):
    """Start stock of each open plant, the end target and, with historical_hydro_path, the path targets of each
    reservoir group, from the BFE weekly stocks, with the files read and the assumptions. A group is all open plants
    (TotalCH) or, with historical_hydro_regions, a BFE region. The group's observed fill fraction at the period start
    instant (before the first hour) and at the end instant (after the last hour) times the model storage of its open
    plants; the start stock is split by equal fill fraction within the group."""
    mode = settings["historical_hydro_end"]
    if mode not in HYDRO_END_MODES:
        raise ValueError(f"historical_hydro_end must be one of {list(HYDRO_END_MODES)}, got {mode!r}")
    path_mode = settings["historical_hydro_path"]
    every_hours = isinstance(path_mode, int) and not isinstance(path_mode, bool) and path_mode > 0
    if not (path_mode is False or path_mode == "bfe_weekly" or every_hours):
        raise ValueError(f"historical_hydro_path must be False, 'bfe_weekly' or a positive whole number of hours, "
                         f"got {path_mode!r}")
    s = scenario_name
    storage = {p: fleet["Plant_capacity_strg"][(p, s)] for p in fleet_inflow_shares(settings)}
    group_of = reservoir_groups(settings, list(storage))
    groups = [g for g in (NATIONAL_GROUP, *BFE_REGIONS.values(), OTHER_REGION) if g in group_of.values()]
    columns = ["date", *[f"{g}_{suffix}" for g in groups for suffix in ("GWh", "max_GWh")]]
    path = processed_table(settings, RESERVOIR_TABLE, columns)
    table = pd.read_csv(path, usecols=columns)
    readings = {g: fill_readings(table, g) for g in groups}
    total = {g: sum(capacity for p, capacity in storage.items() if group_of[p] == g) for g in groups}
    start, end = period_bounds_utc(settings["historical_period"])
    start_fill = {g: fill_at(readings[g], start) for g in groups}
    staged = {"Reservoir_group": group_of,
              "Reservoir_start_stock": {(p, s): start_fill[group_of[p]][0] * capacity for p, capacity in storage.items()}}
    assumptions = [
        f"reservoir stocks: BFE weekly stocks ({RESERVOIR_TABLE}) of the reservoir groups {groups}, each reading "
        f"dated Monday for Sunday 24:00 local; fill fraction = <group>_GWh / <group>_max_GWh of the same reading",
        f"start stock at {start} (before the first hour): fill fraction per group "
        f"{ {g: f'{fill:.6f} ({source})' for g, (fill, source) in start_fill.items()} }; times the storage of each "
        f"open plant of the group {storage} MWh (equal fill fraction within a group, an assumption: BFE observes only "
        f"the group's stock)",
    ]
    if mode == "free":
        staged["Reservoir_end_target"] = {s: {"mode": mode, "MWh": None}}
        assumptions.append("end target: none (historical_hydro_end = 'free')")
    else:
        end_fill = {g: fill_at(readings[g], end) for g in groups}
        staged["Reservoir_end_target"] = {s: {"mode": mode, "MWh": {g: end_fill[g][0] * total[g] for g in groups}}}
        assumptions.append(f"end target at {end} (after the last hour), historical_hydro_end = {mode!r}: fill "
                           f"fraction per group { {g: f'{fill:.6f} ({source})' for g, (fill, source) in end_fill.items()} }; "
                           f"times the storage of the group's open plants {total} MWh")
    staged["Reservoir_path_target"] = path_targets(readings, settings, total, s) if path_mode else {}
    count = len(staged["Reservoir_path_target"]) // len(groups)
    if path_mode == "bfe_weekly":
        assumptions.append(f"weekly path: stock of each reservoir group equal to {count} BFE readings inside the "
                           f"period, each after the model step that ends at the reading; fill fraction times the "
                           f"group's storage {total} MWh")
    elif path_mode:
        assumptions.append(f"path every {path_mode} hours: stock of each reservoir group at {count} instants inside "
                           f"the period, every {path_mode} hours from the start; fill fraction linearly interpolated "
                           f"between the surrounding BFE weekly readings (an assumption: BFE does not observe the "
                           f"stock between readings) times the group's storage {total} MWh")
    if settings["historical_hydro_regions"]:
        assumptions.append(f"reservoir groups: the BFE regions, by the canton of each power station ({BFE_REGIONS}, "
                           f"any other canton {OTHER_REGION}; a lake may lie in another canton than its power "
                           f"station): {pd.Series(group_of).value_counts().to_dict()} open plants")
    return staged, [str(path)], assumptions


def read_fixed_units(scenario_name, settings, fleet, inflow, stocks):
    """Plant groups whose storage-hydro output equals the observed output of their ENTSO-E A73 units
    (historical_hydro_fixed_units, a diagnostic), with the files read and the assumptions. Staged: Hydro_fixed_group
    (group -> plants) and Hydro_fixed_output ((group, step, scenario) -> MW) in every hour ENTSO-E published each of
    the group's units; in the other hours the group is free. Raises on a malformed group file, a plant outside the
    WASTA fleet or in two groups, a unit in two groups or missing from UNITS_TABLE, no group to fix, a group without
    a published hour, negative output, output above the group's available capacity by more than UNIT_ROUNDING_MW (up
    to it the output is cut to the capacity), and a group without pumps whose start stock and inflow cannot supply its
    observed output even with no spill."""
    path = settings["historical_hydro_fixed_units"]
    if path is False:
        return {"Hydro_fixed_group": {}, "Hydro_fixed_output": {}}, [], []
    if not isinstance(path, str):
        raise ValueError(f"historical_hydro_fixed_units must be False or the path of a CSV, got {path!r}")
    if settings["historical_hydro_fleet"] != "wasta_individual":
        raise ValueError(f"historical_hydro_fixed_units needs historical_hydro_fleet = 'wasta_individual' (plants "
                         f"ENTSO-E units can report), got {settings['historical_hydro_fleet']!r}")
    if not Path(path).is_file():
        raise ValueError(f"historical_hydro_fixed_units: no such file {path}")
    table = pd.read_csv(path, dtype=str, encoding="utf-8", keep_default_na=False)
    missing = [c for c in UNIT_GROUP_COLUMNS if c not in table.columns]
    if missing:
        raise ValueError(f"{path} lacks the columns {missing}")
    if table["group"].duplicated().any():
        raise ValueError(f"{path}: groups named twice: {sorted(set(table.loc[table['group'].duplicated(), 'group']))}")
    s = scenario_name
    groups, units_of, group_of_plant, group_of_unit = {}, {}, {}, {}
    for i, row in table.iterrows():
        g = row["group"]
        if row["fix"] not in ("yes", "no"):
            raise ValueError(f"{path}, row {i + 2}: fix must be yes or no, got {row['fix']!r}")
        plants, units = (sorted({x.strip() for x in row[c].split(";") if x.strip()}) for c in ("plants", "entsoe_units"))
        if not plants or not units:
            raise ValueError(f"{path}, row {i + 2}: group {g} needs at least one plant and one unit")
        for p in plants:
            if p not in fleet["Plant_list"] or fleet["Map_plant_tech"][p] not in HYDRO_TECHS:
                raise ValueError(f"{path}, row {i + 2}: {p} is no storage-hydro plant of the WASTA fleet")
            if p in group_of_plant:
                raise ValueError(f"{path}: {p} is in the groups {group_of_plant[p]} and {g}")
            group_of_plant[p] = g
        for u in units:
            if u in group_of_unit:
                raise ValueError(f"{path}: unit {u} is in the groups {group_of_unit[u]} and {g}")
            group_of_unit[u] = g
        if row["fix"] == "yes":
            groups[g], units_of[g] = plants, units
    if not groups:
        raise ValueError(f"{path}: no group with fix = yes, so historical_hydro_fixed_units would fix nothing")
    columns = sorted({u for units in units_of.values() for u in units})
    units_path = processed_table(settings, UNITS_TABLE, ["time_utc", *columns])
    observed = pd.read_csv(units_path, usecols=["time_utc", *columns])
    observed.index = pd.to_datetime(observed["time_utc"], utc=True)
    time_map = historical_time_map(settings["historical_period"])
    absent = time_map.loc[~time_map["time_utc"].isin(observed.index), "time_utc"]
    if len(absent):
        raise ValueError(f"{UNITS_TABLE}: {len(absent)} hours of the period are missing, e.g. {absent.iloc[0]}")
    observed = observed.loc[time_map["time_utc"], columns].apply(pd.to_numeric, errors="coerce")
    observed.index = time_map["t"].to_numpy()
    if observed.isin([math.inf, -math.inf]).any().any() or (observed < 0).any().any():
        raise ValueError(f"{UNITS_TABLE}: non-finite or negative output of {list(observed.columns[(observed < 0).any()])}")
    staged = {"Hydro_fixed_group": groups, "Hydro_fixed_output": {}}
    summary, cut = [], {}
    for g, plants in groups.items():
        output = observed[units_of[g]].sum(axis=1, min_count=len(units_of[g]))  # NaN where a unit is unpublished
        published = output.notna()
        if not published.any():
            raise ValueError(f"{UNITS_TABLE}: no published hour of the units of group {g} in the period")
        capacity = sum(pd.Series({t: fleet["Plant_capacity_gen"][(p, s)] * fleet["Avail_plant"][(p, t, s)]
                                  for t in output.index}) for p in plants)
        excess = output - capacity
        if (excess > UNIT_ROUNDING_MW).any():
            t = excess.idxmax()
            raise ValueError(f"group {g}: observed output {output[t]:.1f} MW above its available capacity "
                             f"{capacity[t]:.1f} MW at {t}; set fix = no or check the plants' capacity")
        if (excess > 0).any():
            cut[g] = int((excess > 0).sum())
            output = output.where(~(excess > 0), capacity)
        if not any(fleet["Plant_capacity_pmp"].get((p, s), 0.0) > 0 for p in plants):
            check_fixed_water(g, plants, output.fillna(0.0), fleet, inflow, stocks, s)
        staged["Hydro_fixed_output"].update({(g, t, s): float(output[t]) for t in output.index[published]})
        summary.append(f"{g} ({', '.join(plants)}; units {', '.join(units_of[g])}): {int(published.sum())} of "
                       f"{len(output)} hours published, {output.sum() / 1e3:,.0f} GWh")
    assumptions = [f"fixed plant groups (historical_hydro_fixed_units = {path!r}): output of each group equal to its "
                   f"ENTSO-E A73 units' observed output ({UNITS_TABLE}); free in the hours a unit is unpublished: "
                   + "; ".join(summary)]
    if cut:
        assumptions.append(f"observed output above the group's available capacity by at most {UNIT_ROUNDING_MW} MW "
                           f"(rounding) cut to the capacity, hours per group: {cut}")
    return staged, [path, str(units_path)], assumptions


def check_fixed_water(group, plants, output, fleet, inflow, stocks, scenario_name):
    """Raise unless the group's start stock and inflow can supply its fixed output: the stock, spilling only above
    the group's storage and drawing output at the best discharge efficiency of its plants, stays at or above 0."""
    s = scenario_name
    stock = sum(stocks["Reservoir_start_stock"][(p, s)] for p in plants)
    storage = sum(fleet["Plant_capacity_strg"][(p, s)] for p in plants)
    efficiency = max(fleet["Map_eff_out_plant"][p] for p in plants)
    for t, value in output.items():
        stock = min(storage, stock + sum(inflow["Inflow_data"][(p, t, s)] for p in plants) - value / efficiency)
        if stock < 0:
            raise ValueError(f"group {group}: its observed output needs {-stock:,.0f} MWh more water than its start "
                             f"stock and inflow hold by {t}; set fix = no or check the plants' inflow")


def read_hydro_operation(scenario_name, settings, fleet, fixed_plants=()):
    """The storage-hydro operating constraints of a back-cast (experiments/backcast/SPEC_hydro_operation.md): the
    must-run floor and the upward reserve withheld per scenario, and the outage caps per (plant, step, scenario);
    with the files read and the assumptions. Everything is off unless its setting is given. The turbine cap of a
    plant in fixed_plants (historical_hydro_fixed_units) is its installed capacity: its observed output holds its
    outages already; its pump cap stays."""
    s = scenario_name
    floor = operation_MW(settings, "historical_hydro_floor_MW")
    reserve = operation_MW(settings, "historical_hydro_reserve_up_MW")
    staged = {"Hydro_operation": {s: {"floor_MW": floor, "reserve_up_MW": reserve}}, "Hydro_outage_cap": {}}
    files, assumptions = [], []
    if floor is not None:
        assumptions.append(f"must-run floor: storage-hydro output ({', '.join(HYDRO_TECHS)}) at least {floor} MW "
                           f"every hour (historical_hydro_floor_MW), for units holding downward reserve; pumping "
                           f"while turbining is not prevented, so the floor may be met by both at once; variable-speed "
                           f"pumps holding reserve while pumping are ignored")
    if reserve is not None:
        assumptions.append(f"upward reserve withheld: storage-hydro output at most the hour's available turbine "
                           f"capacity (gen_max x avail_plant, capped by outages) minus {reserve} MW "
                           f"(historical_hydro_reserve_up_MW)")
    path = settings["historical_hydro_outages"]
    if path is not False:
        staged["Hydro_outage_cap"], windows = read_outages(path, settings, fleet, s)
        files.append(path)
        assumptions.append(f"unit outages and limits from {path} (windows in local wall time or UTC, end "
                           f"exclusive, clipped to the period): " + "; ".join(windows))
        lifted = {}
        for (p, t, _), (turbine, pump) in list(staged["Hydro_outage_cap"].items()):
            if p in fixed_plants:
                staged["Hydro_outage_cap"][(p, t, s)] = (fleet["Plant_capacity_gen"][(p, s)], pump)
                lifted[p] = lifted.get(p, 0) + 1
        if lifted:
            assumptions.append(f"turbine caps of the fixed plants (historical_hydro_fixed_units) lifted to their "
                               f"installed capacity, since their observed output holds their outages; pump caps kept; "
                               f"hours per plant: {lifted}")
    return staged, files, assumptions


def operation_MW(settings, name):
    """A hydro-operation setting in MW, or None when it is False."""
    value = settings[name]
    if value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be False or a positive finite number of MW, got {value!r}")
    return float(value)


def read_outages(path, settings, fleet, scenario_name):
    """Outage caps (turbine MW, pump MW) per (plant, step, scenario) for the steps whose hour starts inside a window,
    and one summary per plant. Windows are local wall time (start_local, end_local) or UTC (start_utc, end_utc), end
    exclusive. Raises on a missing file or column, a plant outside the hydro fleet, an unparsable, ambiguous or
    non-existent time, a window ending before it starts, overlapping windows of one plant, a cap above the installed
    capacity, and a window entirely outside the period."""
    if not isinstance(path, str):
        raise ValueError(f"historical_hydro_outages must be False or the path of a CSV, got {path!r}")
    if not Path(path).is_file():
        raise ValueError(f"historical_hydro_outages: no such file {path}")
    table = pd.read_csv(path, dtype=str, encoding="utf-8")
    missing = [c for c in OUTAGE_COLUMNS if c not in table.columns]
    if missing:
        raise ValueError(f"{path} lacks the columns {missing}")
    time_columns = [c for pair in OUTAGE_TIME_COLUMNS.values() for c in pair if c in table.columns]
    pairs = [pair for pair in OUTAGE_TIME_COLUMNS.values() if all(c in table.columns for c in pair)]
    if len(pairs) != 1 or len(time_columns) != 2:
        raise ValueError(f"{path} needs start_local and end_local, or start_utc and end_utc")
    start_column, end_column = pairs[0]
    instant = outage_instant if start_column == "start_local" else outage_instant_utc
    s = scenario_name
    time_map = historical_time_map(settings["historical_period"])
    hours = pd.DatetimeIndex(time_map["time_utc"])
    steps = time_map["t"].to_numpy()
    caps, spans, summary = {}, {}, {}
    for i, row in table.iterrows():
        p = row["plant"]
        if p not in fleet["Plant_list"]:
            raise ValueError(f"{path}, row {i + 2}: {p} is not in the hydro fleet ({settings['historical_hydro_fleet']})")
        start, end = instant(path, i, row[start_column]), instant(path, i, row[end_column])
        if end <= start:
            raise ValueError(f"{path}, row {i + 2}: the window of {p} ends before it starts")
        turbine, pump = (float(row[c]) for c in ("turbine_MW", "pump_MW"))
        if turbine < 0 or pump < 0:
            raise ValueError(f"{path}, row {i + 2}: negative cap for {p}")
        if turbine > fleet["Plant_capacity_gen"][(p, s)]:
            raise ValueError(f"{path}, row {i + 2}: {turbine} MW is above the installed turbine capacity of {p}, "
                             f"{fleet['Plant_capacity_gen'][(p, s)]} MW")
        if pump > fleet["Plant_capacity_pmp"].get((p, s), 0.0):
            raise ValueError(f"{path}, row {i + 2}: {pump} MW is above the installed pump capacity of {p}, "
                             f"{fleet['Plant_capacity_pmp'].get((p, s), 0.0)} MW")
        spans.setdefault(p, []).append((start, end))
        first, last = hours.searchsorted(start), hours.searchsorted(end)  # the hours starting in [start, end)
        if first >= last:
            raise ValueError(f"{path}, row {i + 2}: the window of {p} lies entirely outside the period "
                             f"{settings['historical_period']}")
        caps.update({(p, t, s): (turbine, pump) for t in steps[first:last]})
        entry = summary.setdefault(p, {"windows": 0, "inside": 0, "hours": 0.0, "turbine": [], "pump": [],
                                       "sources": []})
        entry["windows"] += 1
        entry["inside"] += int(last - first)
        entry["hours"] += (end - start) / pd.Timedelta(hours=1)
        entry["turbine"].append(turbine)
        entry["pump"].append(pump)
        entry["sources"].append(row["source"])
    for p, windows in spans.items():  # sorted by start, any overlap shows between two neighbours
        ordered = sorted(windows)
        if any(later[0] < earlier[1] for earlier, later in zip(ordered, ordered[1:])):
            raise ValueError(f"{path}: windows of {p} overlap")
    return caps, [outage_summary(p, entry) for p, entry in summary.items()]


def outage_summary(plant, entry):
    """One line per plant: its windows, their hours in the period and in all, the cap ranges and the sources."""
    def span(values):
        low, high = min(values), max(values)
        return f"{low} MW" if low == high else f"{low}-{high} MW"
    plural = "s" if entry["windows"] > 1 else ""
    sources = "; ".join(dict.fromkeys(entry["sources"]))
    return (f"{plant}: {entry['windows']} window{plural}, {entry['inside']} of {int(entry['hours'])} hours in the "
            f"period, turbines {span(entry['turbine'])}, pumps {span(entry['pump'])} ({sources})")


def outage_instant_utc(path, i, text):
    """A UTC time 'YYYY-MM-DD HH:MM' of the outage table as a UTC instant."""
    try:
        return pd.to_datetime(text, format=OUTAGE_TIME_FORMAT).tz_localize("UTC")
    except (ValueError, TypeError) as error:
        raise ValueError(f"{path}, row {i + 2}: unparsable time {text!r}, expected YYYY-MM-DD HH:MM") from error


def outage_instant(path, i, text):
    """A local wall time 'YYYY-MM-DD HH:MM' of the outage table as a UTC instant."""
    try:
        local = pd.to_datetime(text, format=OUTAGE_TIME_FORMAT)
    except (ValueError, TypeError) as error:
        raise ValueError(f"{path}, row {i + 2}: unparsable time {text!r}, expected YYYY-MM-DD HH:MM") from error
    try:
        return local.tz_localize(LOCAL_TZ, ambiguous="raise", nonexistent="raise").tz_convert("UTC")
    except InvalidTimeError as error:  # pytz's AmbiguousTimeError and NonExistentTimeError
        raise ValueError(f"{path}, row {i + 2}: local time {text} is ambiguous or does not exist (clock change)") from error


def reservoir_groups(settings, plants):
    """The reservoir group of each open plant: TotalCH, or with historical_hydro_regions = 'bfe_regions' the BFE
    region of its power station's canton (WASTA fleet only)."""
    mode = settings["historical_hydro_regions"]
    if mode is False:
        return {p: NATIONAL_GROUP for p in plants}
    if mode != "bfe_regions":
        raise ValueError(f"historical_hydro_regions must be False or 'bfe_regions', got {mode!r}")
    if settings["historical_hydro_fleet"] != "wasta_individual":
        raise ValueError(f"historical_hydro_regions = 'bfe_regions' needs historical_hydro_fleet = 'wasta_individual' "
                         f"(the cantons of {WASTA_PLANTS}), got {settings['historical_hydro_fleet']!r}")
    table = pd.read_csv(WASTA_PLANTS, encoding="utf-8-sig")
    canton = {wasta_plant(c, n): k for c, n, k in zip(table["category"], table["name"], table["canton"])}
    missing = [p for p in plants if not isinstance(canton.get(p), str)]
    if missing:
        raise ValueError(f"{WASTA_PLANTS}: no canton for the open plants {missing[:5]}")
    return {p: BFE_REGIONS.get(canton[p], OTHER_REGION) for p in plants}


def path_targets(readings, settings, total, scenario_name):
    """Stock of each reservoir group's open plants strictly inside the period, keyed (model step that ends at the
    instant, group, scenario), in time order and then by group: the group's fill fraction times its storage. With
    'bfe_weekly', at every BFE reading; with a number of hours N, every N hours from the start, interpolated between
    the readings (fill_at)."""
    mode = settings["historical_hydro_path"]
    start, end = period_bounds_utc(settings["historical_period"])
    time_map = historical_time_map(settings["historical_period"])
    step_ending = dict(zip(pd.DatetimeIndex(time_map["time_utc"]) + pd.Timedelta(hours=1), time_map["t"]))
    inside = {}
    for g, group_readings in readings.items():
        if mode == "bfe_weekly":
            inside[g] = group_readings[(group_readings.index > start) & (group_readings.index < end)]
            if inside[g].empty:
                raise ValueError(f"historical_hydro_path: no BFE reading lies inside the period "
                                 f"{settings['historical_period']}")
        else:
            instants = pd.date_range(start + pd.Timedelta(hours=mode), end, freq=f"{mode}h", inclusive="left")
            if instants.empty:
                raise ValueError(f"historical_hydro_path = {mode}: no target instant lies inside the period "
                                 f"{settings['historical_period']}")
            inside[g] = pd.Series([fill_at(group_readings, instant)[0] for instant in instants], index=instants)
    instants = sorted(set().union(*(series.index for series in inside.values())))
    uneven = [g for g, series in inside.items() if list(series.index) != instants]
    if uneven:
        raise ValueError(f"historical_hydro_path: the reservoir groups {uneven} lack readings at some target instants")
    off_hour = [instant for instant in instants if instant not in step_ending]
    if off_hour:
        raise ValueError(f"historical_hydro_path: target instants {off_hour[:3]} do not fall at the end of a model "
                         f"hour")
    return {(step_ending[instant], g, scenario_name): float(inside[g][instant]) * total[g]
            for instant in instants for g in readings}


def fill_readings(table, group=NATIONAL_GROUP):
    """Fill fraction of each BFE reading of one reservoir group (a column prefix of the BFE table), indexed by its
    UTC instant (the Monday date is Sunday 24:00 local)."""
    stock, capacity = f"{group}_GWh", f"{group}_max_GWh"
    instants = pd.to_datetime(table["date"]).dt.tz_localize(LOCAL_TZ).dt.tz_convert("UTC")
    if instants.duplicated().any():
        raise ValueError(f"{RESERVOIR_TABLE}: duplicate dates {sorted(table.loc[instants.duplicated(), 'date'])}")
    valid = table[stock].notna() & table[capacity].notna()
    not_positive = table.loc[valid & ~(table[capacity] > 0), "date"]
    if len(not_positive):
        raise ValueError(f"{RESERVOIR_TABLE}: {capacity} is not positive on {list(not_positive)}")
    fill = (table[stock] / table[capacity])[valid]
    return pd.Series(fill.to_numpy(), index=pd.DatetimeIndex(instants[valid])).sort_index()


def fill_at(readings, instant):
    """Fill fraction at an instant: a reading at that instant, else the linear interpolation between the two
    surrounding readings, at most MAX_READING_GAP apart; no extrapolation. Also a description of the source."""
    if instant in readings.index:
        return float(readings[instant]), f"reading at {instant}"
    before, after = readings[readings.index < instant], readings[readings.index > instant]
    if before.empty or after.empty:
        raise ValueError(f"{RESERVOIR_TABLE}: no reading {'before' if before.empty else 'after'} {instant}; "
                         f"stocks are not extrapolated")
    (t0, f0), (t1, f1) = (before.index[-1], before.iloc[-1]), (after.index[0], after.iloc[0])
    if t1 - t0 > MAX_READING_GAP:
        raise ValueError(f"{RESERVOIR_TABLE}: the readings around {instant}, {t0} and {t1}, are more than "
                         f"{MAX_READING_GAP.days} days apart")
    fill = f0 + (f1 - f0) * ((instant - t0) / (t1 - t0))
    return float(fill), f"interpolated between the readings at {t0} ({f0:.6f}) and {t1} ({f1:.6f})"


def installed_capacity(settings, column):
    """Installed power of one category at every model step (index: model step), and the published year used for
    each local year of the period: for each hour, the value of the latest published year not after the hour's local
    year. The table gives the installed power at the end of each year."""
    path = processed_table(settings, INSTALLED_TABLE, ["year", column])
    table = pd.read_csv(path, usecols=["year", column])
    if table["year"].duplicated().any():
        raise ValueError(f"{INSTALLED_TABLE}: duplicate years {sorted(table.loc[table['year'].duplicated(), 'year'])}")
    values = table.set_index("year")[column]
    bad = values.isna() | values.isin([math.inf, -math.inf])
    if bad.any():
        raise ValueError(f"{INSTALLED_TABLE}: missing or non-finite values of {column} in {sorted(values.index[bad])}")
    time_map = historical_time_map(settings["historical_period"])
    local_years = time_map["time_local"].dt.year
    years = {}
    for year in sorted(set(local_years)):
        published = [y for y in values.index if y <= year]
        if not published:
            raise ValueError(f"{INSTALLED_TABLE}: no published year up to {year} for {column} "
                             f"(published: {sorted(values.index)})")
        years[int(year)] = int(max(published))
    capacity = pd.Series([float(values[years[y]]) for y in local_years], index=time_map["t"].to_numpy())
    return capacity, years


def read_processed_hourly(settings, file_name, columns):
    """The given columns of one processed hourly table at every model step of the period (index: model step).

    Raises on a missing file or column, a duplicate column or hour, a missing hour or a missing or non-finite
    value, naming the series and the hours.
    """
    path = processed_table(settings, file_name, ["time_utc", *columns])
    table = pd.read_csv(path, usecols=["time_utc", *columns])
    table.index = pd.to_datetime(table["time_utc"], utc=True)
    if table.index.duplicated().any():
        raise ValueError(f"{file_name}: duplicate hours, e.g. {table.index[table.index.duplicated()][0]}")
    time_map = historical_time_map(settings["historical_period"])
    absent = time_map.loc[~time_map["time_utc"].isin(table.index), "time_utc"]
    if len(absent):
        raise ValueError(f"{file_name}: {len(absent)} hours of the period are missing, e.g. {absent.iloc[0]}")
    values = table.loc[time_map["time_utc"], list(columns)].apply(pd.to_numeric, errors="coerce")
    for column in columns:
        bad = values[column].isna() | values[column].isin([math.inf, -math.inf])
        if bad.any():
            raise ValueError(f"{file_name}: {int(bad.sum())} missing or non-finite values of {column}, "
                             f"e.g. at {values.index[bad.to_numpy()][0]}")
    values.index = time_map["t"].to_numpy()
    return values


def processed_table(settings, file_name, columns):
    """Path of one table of the processed folder; raises unless it exists and its header holds each of the given
    columns exactly once."""
    path = Path(settings["historical_data_dir"]) / file_name
    if not path.exists():
        raise FileNotFoundError(f"{path} not found (historical_data_dir = {settings['historical_data_dir']!r})")
    with open(path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    duplicated = sorted({column for column in header if header.count(column) > 1})
    if duplicated:
        raise ValueError(f"{file_name}: duplicate columns {duplicated}")
    missing = [column for column in columns if column not in header]
    if missing:
        raise ValueError(f"{file_name}: no column {missing}")
    return path


def read_structure(scenario_name):
    """CH00, its fixed consumer and the four border lines (limits and prices come from the market inputs)."""
    lines = [f"HVAC_{neighbour}_{NODE}" for neighbour in BORDER_NEIGHBOURS]
    return {
        "Consumer_list": [CONSUMER],
        "Map_consumer_node": {CONSUMER: NODE},
        "Map_node_consumer": {NODE: [CONSUMER]},
        "LineATC_list": lines,
        "Map_line_node": {line: {"start_node": n, "end_node": NODE} for line, n in zip(lines, BORDER_NEIGHBOURS)},
        "Map_node_importinglineATC": {NODE: list(lines)},
        "Map_node_exportinglineATC": {NODE: []},
    }


def read_hydro_fleet(scenario_name, settings):
    """The representative hydro fleet with its capacities, efficiencies, start conditions, availability and costs,
    all independent of run year and weather year; and the assumptions behind them."""
    from model.data_import_fcns import read_hydro_capacities
    from model.structural_parameters import Map_tech_startcondition

    fleet_name = settings["historical_hydro_fleet"]
    if fleet_name == "representative_2030":
        plants, plant_tech, plant_node, gen_max, storage, pumping, consumer_plants = read_hydro_capacities(
            [NODE], True, settings["merge_some_countries"], settings["target_merge_countries"], False, False
        )
        fleet_assumption = "hydro fleet: the repository's representative 2030 fleet, not observed 2025/26 capacities"
    elif fleet_name == "wasta_individual":
        plants, plant_tech, plant_node, gen_max, storage, pumping, consumer_plants, fleet_assumption = wasta_fleet()
    else:
        raise ValueError(f"historical_hydro_fleet must be one of {list(HYDRO_FLEETS)}, got {fleet_name!r}")
    techs = sorted(set(plant_tech.values()))
    eff_in = year_independent("efficiency_into_storage", techs)
    eff_out = year_independent("efficiency", techs)
    om_cost = year_independent("om_cost_eurMWH", techs)
    emission = year_independent("emission_factor", techs)
    availability = hydro_availability(techs)
    availability_mode = settings["historical_hydro_availability"]
    if availability_mode == "outage_messages":
        if settings["historical_hydro_outages"] is False:
            raise ValueError("historical_hydro_availability = 'outage_messages' needs historical_hydro_outages, the "
                             "outages that replace the availability factor")
        availability_source = (f"1.0: every outage comes from historical_hydro_outages "
                               f"({settings['historical_hydro_outages']}), in place of the availability table's "
                               f"{availability}")
        availability = {tech: 1.0 for tech in techs}
    elif availability_mode == "workbook":
        availability_source = f"{availability} (availability table, constant over the months)"
    else:
        raise ValueError(f"historical_hydro_availability must be 'workbook' or 'outage_messages', got "
                         f"{availability_mode!r}")
    open_plants = list(fleet_inflow_shares(settings))
    outside = [p for p in open_plants if p not in plants]
    if outside:
        raise ValueError(f"open plants {outside} are not in the hydro fleet {list(plants)} "
                         f"(historical_hydro_fleet = {fleet_name!r})")
    # Electrical basis: inflow and stocks are electrical energy, so an open plant discharges with efficiency 1 and
    # charges with the workbook round trip; the closed loop and the dam keep the workbook values
    basis_in = {p: eff_in[plant_tech[p]] * eff_out[plant_tech[p]] if p in open_plants else eff_in[plant_tech[p]]
                for p in plants}
    basis_out = {p: 1.0 if p in open_plants else eff_out[plant_tech[p]] for p in plants}
    open_in = {p: basis_in[p] for p in open_plants}
    s = scenario_name
    fleet = {
        "Plant_list": list(plants),
        "Map_plant_tech": dict(plant_tech),
        "Map_plant_node": dict(plant_node),
        "Map_consumer_plant": {consumer: list(p) for consumer, p in consumer_plants.items()},
        "Plant_capacity_gen": {(p, s): value for p, value in gen_max.items()},
        "Plant_capacity_pmp": {(p, s): value for p, value in pumping.items()},
        "Plant_capacity_strg": {(p, s): value for p, value in storage.items()},
        "Map_eff_in_plant": basis_in,
        "Map_eff_out_plant": basis_out,
        "Map_plant_startcondition": {p: Map_tech_startcondition[plant_tech[p]] for p in plants},
        "Avail_plant": {(p, t, s): availability[plant_tech[p]] for p in plants for t in settings["T_list"]},
        "cost_data_opr_slp": {(tech, s): om_cost[tech] for tech in techs},
        "cost_data_opr_int": {(tech, s): 0.0 for tech in techs},
        "op_cost_n_tech_calibration": {(NODE, tech, s): 1.0 for tech in techs},
        "emission_factor_per_MWh": {(tech, s): emission[tech] / eff_out[tech] for tech in techs},
        # investment is off, so every slope is exactly zero
        "cost_data_inv_gen_slp": {(tech, s): 0.0 for tech in techs},
        "cost_data_inv_e_slp": {(tech, s): 0.0 for tech in techs},
        "cost_data_inv_discharge_slp": {(tech, s): 0.0 for tech in techs},
    }
    assumptions = [
        fleet_assumption,
        f"hydro availability {availability_source}",
        f"storage efficiencies in {eff_in}, out {eff_out} and O&M {om_cost} EUR/MWh: cost-workbook values, "
        f"identical for {WORKBOOK_YEARS}",
        f"electrical basis of the open plants {open_plants}: discharge efficiency 1 and charging efficiency "
        f"{open_in} (workbook in x out), so the round trip is unchanged; the fleet "
        f"table's storage (sto_GWh) is taken as electrical energy, as the ENTSO-E methodology strongly implies "
        f"without stating it",
    ]
    return fleet, assumptions


def wasta_fleet():
    """The storage plants of the WASTA 2025/26 statistics, one model plant each (experiment): dams, open and closed
    pumped storage at CH00, with their power, pumping and storage capacities. Run-of-river comes from observed
    generation, and pumping stations without turbines or storage are left out (declared)."""
    table = pd.read_csv(WASTA_PLANTS, encoding="utf-8-sig")
    kept = table[table["category"].isin(["dam", "psp_open", "psp_close"])]
    plants = [wasta_plant(row["category"], row["name"]) for _, row in kept.iterrows()]
    if len(set(plants)) != len(plants):
        raise ValueError(f"{WASTA_PLANTS}: two plants share the model name of a WASTA plant")
    plant_tech = {p: category for p, category in zip(plants, kept["category"])}
    gen_max = dict(zip(plants, kept["power_capacity_MW"].astype(float)))
    storage = dict(zip(plants, kept["storage_capacity_GWh"].astype(float) * 1000))
    pumping = {p: float(mw) for p, mw, category in zip(plants, kept["pump_capacity_MW"], kept["category"])
               if category != "dam"}
    left_out = table[~table["category"].isin(["dam", "psp_open", "psp_close"])]
    pump_out = left_out.groupby("category")["pump_capacity_MW"].sum().round(1).to_dict()
    assumption = (f"hydro fleet: WASTA 2025/26 storage plants, one model plant each ({len(plants)}: "
                  f"{kept['category'].value_counts().to_dict()}), {kept['power_capacity_MW'].sum():.0f} MW, "
                  f"{kept['storage_capacity_GWh'].sum():.0f} GWh; left out: run-of-river (observed generation) and "
                  f"pumping without turbines or storage, pumps by category {pump_out} MW; each plant has its own "
                  f"reservoir, cascades are not linked")
    return plants, plant_tech, {p: NODE for p in plants}, gen_max, storage, pumping, {CONSUMER: plants}, assumption


def wasta_plant(category, name):
    """Model name of a WASTA plant: its category and its name in ASCII letters, digits and underscores."""
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return f"{category}_{re.sub(r'[^A-Za-z0-9]+', '_', ascii_name).strip('_')}"


def fleet_inflow_shares(settings):
    """The inflow share of each open plant of the chosen fleet."""
    if settings["historical_hydro_fleet"] == "representative_2030":
        return inflow_shares()
    donors = pd.read_csv(WASTA_INFLOW, encoding="utf-8-sig")
    total = donors["nat_inflow_GWh"].sum()
    if not total > 0 or donors["nat_inflow_GWh"].isna().any():
        raise ValueError(f"{WASTA_INFLOW}: natural inflow must be given for every plant and sum to more than 0")
    return {wasta_plant(c, n): float(v / total)
            for c, n, v in zip(donors["category"], donors["name"], donors["nat_inflow_GWh"])}


def fleet_files(settings):
    if settings["historical_hydro_fleet"] == "representative_2030":
        return list(FLEET_TABLES)
    return [WASTA_PLANTS, WASTA_INFLOW]


def inflow_shares():
    """One inflow share per open plant of REPRESENTATIVE_TABLE, which repeats it on each of the plant's rows; raises
    if a plant's rows disagree or the shares do not sum to 1."""
    table = pd.read_csv(REPRESENTATIVE_TABLE)
    shares, problems = {}, []
    for plant, rows in table.groupby("name", sort=False):
        values = sorted(set(rows["inflow_share"]))
        if len(values) != 1:
            problems.append(f"the rows of {plant} disagree on inflow_share ({values})")
        shares[plant] = float(values[0])
    if not math.isclose(sum(shares.values()), 1.0, abs_tol=1e-9):
        problems.append(f"the shares {shares} sum to {sum(shares.values()):.9g}, not 1")
    if problems:
        raise ValueError(f"{REPRESENTATIVE_TABLE}: " + "; ".join(problems))
    return shares


def year_independent(component, techs):
    """Cost-workbook value of each technology, which must be the same in every workbook year."""
    import input.cost_operation_invest_data as operation_data

    values = {}
    for tech in techs:
        by_year = operation_data.cost_component[component].get(tech)
        if not isinstance(by_year, dict) or any(year not in by_year for year in WORKBOOK_YEARS):
            raise ValueError(f"{COST_WORKBOOK}: no {component} of {tech} for {WORKBOOK_YEARS}")
        distinct = {by_year[year] for year in WORKBOOK_YEARS}
        if len(distinct) != 1:
            raise ValueError(f"{COST_WORKBOOK}: {component} of {tech} differs between {WORKBOOK_YEARS} ({by_year}); "
                             f"a back-cast uses only workbook values that do not depend on the year")
        values[tech] = distinct.pop()
    return values


def hydro_availability(techs):
    """Availability of each technology as the normal import gives it: the Swiss row of the availability table,
    else its Default row. It must be the same in every month, since back-cast model steps are no calendar months."""
    table = pd.read_csv(AVAILABILITY_TABLE)
    months = [column for column in table.columns if column not in ("country", "technology")]
    values = {}
    for tech in techs:
        rows = table[(table["technology"] == tech) & (table["country"] == "CH")]
        if rows.empty:
            rows = table[(table["technology"] == tech) & (table["country"] == "Default")]
        if len(rows) != 1:
            raise ValueError(f"{AVAILABILITY_TABLE}: expected one CH or Default row for {tech}, found {len(rows)}")
        monthly = set(rows[months].iloc[0])
        if len(monthly) != 1:
            raise ValueError(f"{AVAILABILITY_TABLE}: the availability of {tech} varies by month ({sorted(monthly)}); "
                             f"back-cast model steps are no calendar months")
        values[tech] = monthly.pop()
    return values


def stage(staged, entries):
    """Add one input's entries (global name -> entries) to the staged inputs; two inputs never share a key."""
    for name, payload in entries.items():
        if name not in staged:
            staged[name] = payload
        elif isinstance(payload, dict):
            shared = set(staged[name]) & set(payload)
            if shared:
                raise ValueError(f"two back-cast inputs write the same keys of {name}: {sorted(shared)[:3]}")
            staged[name] = {**staged[name], **payload}
        else:
            staged[name] = [*staged[name], *payload]


def write_staged(staged):
    """Write the staged inputs into the global lists and dicts the model reads."""
    import data_prep.definitions_common as common

    for name, payload in staged.items():
        container = getattr(common, name)
        if isinstance(container, dict):
            container.update(payload)
        else:
            container.extend(payload)


def check_no_foreign_state(scenario_name):
    """The data lists and dicts must be empty when a back-cast's import starts. In the back-cast's own process
    nothing writes them before, so any entry comes from another meta-scenario run in the same process."""
    import data_prep.definitions_common as common
    import data_prep.definitions_TYNDP as tyndp
    from model.reset_items import get_lists_and_dicts

    filled = []
    for module in (common, tyndp):
        lists, dicts = get_lists_and_dicts(module, [])
        filled += [f"{module.__name__}.{name}" for name in lists + dicts if len(getattr(module, name))]
    if filled:
        raise RuntimeError(f"{scenario_name}: data of another meta-scenario is already loaded in this process "
                           f"({filled[:5]}); run each back-cast in its own process, as run_scenarios.py does")


def inputs_manifest(scenario_name, settings, files, assumptions):
    """What a back-cast used: its period and time steps, every file read with its checksum and columns, the
    assumptions, and the code version."""
    start, end = period_bounds_utc(settings["historical_period"])

    def git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()

    return {
        "scenario": scenario_name,
        "historical_period": settings["historical_period"],
        "period_utc": [start.isoformat(), end.isoformat()],
        "model_steps": [settings["t_start"], settings["t_end"]],
        "historical_data_dir": settings["historical_data_dir"],
        "files": [file_record(path) for path in files],
        "assumptions": assumptions,
        "code": {"git_commit": git("rev-parse", "HEAD"), "uncommitted_changes": bool(git("status", "--porcelain"))},
    }


def file_record(path):
    record = {"path": str(path), "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest()}
    if str(path) == BFE_BALANCE:
        lines, header = bfe_lines()
        record["columns"] = lines[header].split(",")
    elif str(path).endswith(".csv"):
        record["columns"] = list(pd.read_csv(path, nrows=0, comment="#").columns)
    return record


def write_run_records(output_dir, sub_scenarios_list):
    """Write the time map and the inputs manifest of a back-cast into its run folder; other runs have none."""
    for sub_scenario in sub_scenarios_list:
        records = RUN_RECORDS.get(sub_scenario)
        if records is None:
            continue
        records["time_map"].to_csv(Path(output_dir) / "historical_time_map.csv", index=False)
        manifest = json.dumps(records["manifest"], indent=1, ensure_ascii=False)
        (Path(output_dir) / "historical_inputs_manifest.json").write_text(manifest, encoding="utf-8")
