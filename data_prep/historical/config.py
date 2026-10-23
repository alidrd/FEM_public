"""Zones, border directions, source endpoints and paths for the historical CH data pipeline.

All timestamps in processed files are UTC. Periods are given as local (Europe/Zurich) calendar
days, inclusive at both ends, e.g. 2023-10-01 .. 2026-09-30 for three hydrological years.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = REPO_ROOT / "input" / "historical" / "raw"
PROCESSED_DIR = REPO_ROOT / "input" / "historical" / "processed"
PLOTS_DIR = REPO_ROOT / "plots"

LOCAL_TZ = "Europe/Zurich"

# FEM node -> bidding zone code per source. CH and its four neighbours as used in CH_only mode.
ZONES = {
    "CH00": {"energy_charts": "CH", "entsoe": "10YCH-SWISSGRIDZ", "label": "CH"},
    "DE00": {"energy_charts": "DE-LU", "entsoe": "10Y1001A1001A82H", "label": "DE-LU"},
    "FR00": {"energy_charts": "FR", "entsoe": "10YFR-RTE------C", "label": "FR"},
    "IT00": {"energy_charts": "IT-North", "entsoe": "10Y1001A1001A73I", "label": "IT-North"},
    "AT00": {"energy_charts": "AT", "entsoe": "10YAT-APG------L", "label": "AT"},
}
NEIGHBOURS = ["DE00", "FR00", "IT00", "AT00"]

# Border directions, named <exporting>_to_<importing> with two-letter country codes.
# These match the TimeSeriesIdentification in Swissgrid's D-2 NTC files.
COUNTRY_OF_NODE = {"CH00": "CH", "DE00": "DE", "FR00": "FR", "IT00": "IT", "AT00": "AT"}
DIRECTIONS = [
    f"{a}_to_{b}"
    for n in NEIGHBOURS
    for a, b in (("CH", COUNTRY_OF_NODE[n]), (COUNTRY_OF_NODE[n], "CH"))
]

# Rule for turning sub-hourly transfer capacities (NTC) into one hourly limit:
#   "min"  - FEM has one constant flow per hour, so it must respect every quarter-hour limit
#            (also matches hourly capacity products); the matching price rule is the hourly mean.
#   "mean" - the hourly value is an energy total and flows may differ between quarter-hours.
# Prices and power are always averaged. With hourly source limits both rules give the same value.
CAPACITY_HOURLY_RULE = "min"

# Local days on which ENTSO-E replaces Swissgrid D-2 NTC in all eight directions, because Swissgrid's public
# file for the day is an early revision (2026-09-24: version 1 with "n.y.d."). Chosen by the user on
# 2026-10-09; the web copy for it is in raw/entsoe_web/part_2026-09-24.txt.
NTC_ENTSOE_WEB_PREFERRED_DAYS = ("2026-09-24",)
# ENTSO-E day-ahead NTC sources in order of use, both the same product (A61): the API download
# (entsoe_ntc.py, needs a token) before the manual web copy (entsoe_web.py). They fill hours without a
# Swissgrid D-2 value; on NTC_ENTSOE_WEB_PREFERRED_DAYS the first one complete for the day is used.
NTC_ENTSOE_SOURCE_ORDER = ("entsoe_api", "entsoe_web")

ENERGY_CHARTS_API ="https://api.energy-charts.info/v2"
# Energy-Charts allows 2 requests per minute (HTTP 429 above that).
ENERGY_CHARTS_MIN_INTERVAL_S = 31

SWISSGRID_PAGES = {
    "energy_overview": "https://www.swissgrid.ch/en/home/operation/grid-data/transmission.html",
    "cross_border_flows": "https://www.swissgrid.ch/en/home/operation/grid-data/cross-border-load-flows.html",
    "ntc_d2": "https://www.swissgrid.ch/en/home/customers/topics/congestion-mgmt/ntc/d-2-ntc.html",
}

# BFE's own open-data host. The uvek-gis.admin.ch mirror used before 2026-10-09 lagged: on 9 Oct 2026 it
# lacked the 5 Oct 2026 reading that bfe-ogd.ch already served (identical values otherwise).
BFE_RESERVOIR_URL = "https://www.bfe-ogd.ch/ogd17/ogd17_fuellungsgrad_speicherseen.csv"
# Share of on-site consumption (BFE Endverbrauch minus Swissgrid end-user consumption) that is placed in
# the hours like PV output; the rest is spread flat over the month. ASSUMPTION, not an observation: the
# split between self-consumed PV and other on-site generation is not published. 0.5 is the midpoint
# between the two extremes 0 (all flat) and 1 (all PV-shaped); chosen by the user on 2026-10-09 to avoid
# running both extremes. See demand.py.
ONSITE_SOLAR_SHARE = 0.5

# Monthly national electricity balance (GWh): production by type, storage pumping, imports/exports,
# Endverbrauch (end use), Verluste (grid losses), Landesverbrauch = Endverbrauch + Verluste.
BFE_BALANCE_URL = "https://www.bfe-ogd.ch/ogd35/ogd35_schweizerische_elektrizitaetsbilanz_monatswerte.csv"

ENTSOE_API = "https://web-api.tp.entsoe.eu/api"
ENTSOE_TOKEN_ENV = "ENTSOE_API_KEY"
# Source of prices_day_ahead_hourly.csv, the price table back-casts read: "entsoe" (A44) or "energy_charts"
# (SMARD). The user asked for ENTSO-E over SMARD on 2026-10-23. The other source is written as
# prices_day_ahead_<source>_hourly.csv, and validation_report.csv compares the two (equal in every hour of
# Oct 2025 - Sep 2026).
PRICE_SOURCE = "entsoe"
PRICE_SOURCES = ("entsoe", "energy_charts")

# Net electrical capacity of the Swiss nuclear units (MW), unchanged since Muehleberg shut down
# in Dec 2019. Only the total is used; process.py checks it against Energy-Charts installed_power
# for every year Energy-Charts publishes and raises on a mismatch.
NUCLEAR_UNITS_NET_MW = {
    "Beznau 1": 365.0,
    "Beznau 2": 365.0,
    "Goesgen": 1010.0,
    "Leibstadt": 1233.0,
}

# Energy-Charts installed_power categories. The generation series "others" of public_power covers every
# category except hydro, nuclear, wind and solar; ch_installed_power_yearly.csv carries their sum as "others".
# process.py raises on a category in neither list, so a new category is classified before it is used.
INSTALLED_POWER_OTHERS = (
    "conventional_thermal",
    "landfill_gas",
    "waste_incinerators_without_heat_extraction",
    "waste_incinerators_with_heat_extraction",
    "industry",
    "district_heating_plants",
    "small_chp",
)
INSTALLED_POWER_NOT_OTHERS = (
    "hydro_run_of_river",
    "hydro_water_reservoir",
    "hydro_pumped_storage",
    "hydro_pumped_storage_w_o_natural_inflow",
    "nuclear",
    "wind_onshore",
    "wind_offshore",
    "solar_dc",
)

USER_AGENT = "FEM-historical-data/1.0 (research use; Future Markets model)"

# Copernicus PECD 4.2 hydro inflow (CDS dataset sis-energy-pecd, ERA5 reanalysis, zone level), from archives
# downloaded by hand and registered with `python -m data_prep.historical.pecd register <archive.zip>`.
# HRI = reservoir inflow, HOL = open-loop pumped-storage inflow; run of river (HRO) comes from Energy-Charts.
PECD_ZONE = "CH00"
PECD_SERIES = ("HRI", "HOL")
