"""Back-casts: CH_only optimisations of a past period, driven by inputs observed or reconstructed for that
period (glossary: CONTEXT.md; design: CH_ONLY_HISTORICAL_MODEL_PLAN.md; guide: docs/backcast.md).

This module holds the back-cast's time base and setting checks, and the historical import that replaces the
normal data import. It reads processed files only and never imports pipeline code (data_prep/historical).
"""
import re

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
    """Fill the model's global lists and dicts from processed historical files instead of the normal import."""
    raise NotImplementedError(f"{scenario_name}: back-cast import not built yet; the settings and the time base "
                              f"are checked, the historical inputs follow")
