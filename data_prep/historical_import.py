"""Back-casts: CH_only optimisations of a past period, driven by inputs observed or reconstructed for that
period (glossary: CONTEXT.md; design: CH_ONLY_HISTORICAL_MODEL_PLAN.md; guide: docs/backcast.md).

This module holds the back-cast's time base and setting checks, and the historical import that replaces the
normal data import. It reads processed files only and never imports pipeline code (data_prep/historical).
"""
import csv
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

import pandas as pd

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
AVAILABILITY_TABLE = "input/availability_factor_conventional.csv"
COST_WORKBOOK = "input/cost_assumptions.xlsx"
WORKBOOK_YEARS = (2035, 2050)
# Inputs the historical import does not build yet; the step that builds one removes its entry
NOT_YET_BUILT = [
    "renewable infeed",
    "fixed plants",
    "reservoir inflow",
    "reservoir stocks",
]
RUN_RECORDS = {}  # sub-scenario -> time map and inputs manifest, written by core_main into the run folder
# Tables of the processed folder of data_prep/historical (historical_data_dir)
PRICES_TABLE = "prices_day_ahead_hourly.csv"
NTC_TABLE = "ntc_dayahead_combined_hourly.csv"
DEMAND_TABLE = "ch_demand_option_a_hourly.csv"
# Demand columns of DEMAND_TABLE (setting historical_demand) and where each places on-site consumption
DEMAND_PLACEMENT = {
    "demand_MW": "a share {share} (onsite_solar_share_assumed) like PV output, the rest flat",
    "demand_solar_shaped_MW": "all like PV output",
    "demand_flat_MW": "all flat",
}
DEMAND_TYPE = "fixed"  # the only inflexible consumption type of the model (core_main)


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
    files = [*FLEET_TABLES, AVAILABILITY_TABLE, COST_WORKBOOK,
             f"input/LostLoadCost/lostLoadCost_{settings['lost_load_cost_mode']}.csv", *market_files, *demand_files]
    assumptions = [
        f"border lines HVAC_<neighbour>_{NODE} for {', '.join(BORDER_NEIGHBOURS)}, the neighbour as start node",
        *fleet_assumptions,
        "investment is off: every investment slope is 0; operating-cost calibration 1; no limited fuels",
        *market_assumptions,
        *demand_assumptions,
    ]
    return staged, files, assumptions


def read_market(scenario_name, settings):
    """Neighbour prices and day-ahead transfer limits on the four border lines, with the files read and the
    assumptions. For line HVAC_X00_CH00 the upper flow bound (ATC_exportlimit) is X_to_CH and the lower bound
    is minus CH_to_X (ATC_importlimit); NTC_CH_ratio multiplies both directions once."""
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
    return staged, [str(Path(settings["historical_data_dir"]) / PRICES_TABLE), str(ntc_path)], assumptions


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


def read_processed_hourly(settings, file_name, columns):
    """The given columns of one processed hourly table at every model step of the period (index: model step).

    Raises on a missing file or column, a duplicate column or hour, a missing hour or a missing or non-finite
    value, naming the series and the hours.
    """
    path = Path(settings["historical_data_dir"]) / file_name
    if not path.exists():
        raise FileNotFoundError(f"{path} not found (historical_data_dir = {settings['historical_data_dir']!r})")
    with open(path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    duplicated = sorted({column for column in header if header.count(column) > 1})
    if duplicated:
        raise ValueError(f"{file_name}: duplicate columns {duplicated}")
    missing = [column for column in ["time_utc", *columns] if column not in header]
    if missing:
        raise ValueError(f"{file_name}: no column {missing}")
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

    plants, plant_tech, plant_node, gen_max, storage, pumping, consumer_plants = read_hydro_capacities(
        [NODE], True, settings["merge_some_countries"], settings["target_merge_countries"], False, False
    )
    techs = sorted(set(plant_tech.values()))
    eff_in = year_independent("efficiency_into_storage", techs)
    eff_out = year_independent("efficiency", techs)
    om_cost = year_independent("om_cost_eurMWH", techs)
    emission = year_independent("emission_factor", techs)
    availability = hydro_availability(techs)
    s = scenario_name
    fleet = {
        "Plant_list": list(plants),
        "Map_plant_tech": dict(plant_tech),
        "Map_plant_node": dict(plant_node),
        "Map_consumer_plant": {consumer: list(p) for consumer, p in consumer_plants.items()},
        "Map_node_plant": {NODE: list(plants)},
        "Plant_capacity_gen": {(p, s): value for p, value in gen_max.items()},
        "Plant_capacity_pmp": {(p, s): value for p, value in pumping.items()},
        "Plant_capacity_strg": {(p, s): value for p, value in storage.items()},
        "Map_eff_in_plant": {p: eff_in[plant_tech[p]] for p in plants},
        "Map_eff_out_plant": {p: eff_out[plant_tech[p]] for p in plants},
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
        "hydro fleet: the repository's representative 2030 fleet, not observed 2025/26 capacities",
        f"hydro availability {availability} (availability table, constant over the months)",
        f"storage efficiencies in {eff_in}, out {eff_out} and O&M {om_cost} EUR/MWh: cost-workbook values, "
        f"identical for {WORKBOOK_YEARS}",
    ]
    return fleet, assumptions


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
    if str(path).endswith(".csv"):
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
