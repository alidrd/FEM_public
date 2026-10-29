"""Download raw historical data for CH and its neighbours into input/historical/raw/.

Usage (from the repo root):
    python -m data_prep.historical.download --start 2023-10-01 --end 2026-09-30
    python -m data_prep.historical.download --start 2023-10-01 --end 2026-09-30 --sources entsoe entsoe_ntc         --token-file <path of the ENTSO-E token file>
    python -m data_prep.historical.download --start 2025-10-01 --end 2026-07-31 --sources entsoe_units         --token-file <path of the ENTSO-E token file>

entsoe_units (generation per unit, A73) takes one request per day, about 12 s each, so fetch it for the days
a back-cast needs rather than the whole processed period.

Days are local (Europe/Zurich) and inclusive. Files already on disk are kept; pass --force to
fetch them again (needed for files of the running year, which the publishers keep extending).
"""
import argparse

import pandas as pd

from data_prep.historical import bfe, energy_charts, entsoe, entsoe_ntc, swissgrid
from data_prep.historical.config import DIRECTIONS, ZONES
from data_prep.historical.timeutils import year_chunks

PUBLIC_SOURCES = ["energy_charts", "swissgrid", "bfe"]
ALL_SOURCES = PUBLIC_SOURCES + ["entsoe", "entsoe_ntc", "entsoe_units"]


def download_energy_charts(start_day: str, end_day: str, force: bool) -> None:
    print("Energy-Charts (2 requests per minute)")
    energy_charts.download_installed_power("ch", force)
    for first, last in year_chunks(start_day, end_day):
        for zone in ZONES.values():
            energy_charts.download_price(zone["energy_charts"], first, last, force)
        for endpoint in ("public_power", "cbet", "cbpf"):
            energy_charts.download_country(endpoint, "ch", first, last, force)


def download_swissgrid(start_day: str, end_day: str, force: bool) -> None:
    years = range(pd.Timestamp(start_day).year, pd.Timestamp(end_day).year + 1)
    print("Swissgrid energy overview")
    swissgrid.download_listed("energy_overview", [f"EnergieUebersichtCH-{y}.xlsx" for y in years], force)
    print("Swissgrid cross-border flows")
    swissgrid.download_listed("cross_border_flows", [f"Grenzfluesse-{y}.csv" for y in years], force)
    print("Swissgrid D-2 NTC")
    days = pd.date_range(start_day, end_day, freq="D")
    swissgrid.download_listed("ntc_d2", [f"NTC-{d:%Y%m%d}.xml" for d in days], force)


def download_entsoe_ntc(start_day: str, end_day: str, force: bool) -> None:
    print("ENTSO-E day-ahead NTC (A61)")
    entsoe.token()  # fail before any request if the token is missing
    for first, last in year_chunks(start_day, end_day):
        for direction in DIRECTIONS:
            entsoe_ntc.download(direction, first, last, force)


def download_entsoe(start_day: str, end_day: str, force: bool) -> None:
    print("ENTSO-E day-ahead prices (A44) and Swiss generation per type (A75)")
    entsoe.token()  # fail before any request if the token is missing
    for first, last in year_chunks(start_day, end_day):
        for node in ZONES:
            entsoe.download_price(node, first, last, force)
        entsoe.download_generation(first, last, force)


def download_entsoe_units(start_day: str, end_day: str, force: bool) -> None:
    print("ENTSO-E actual generation per unit (A73), one request per day")
    entsoe.token()  # fail before any request if the token is missing
    entsoe.download_generation_units(start_day, end_day, force)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="first local day, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="last local day (inclusive), YYYY-MM-DD")
    parser.add_argument("--sources", nargs="+", choices=ALL_SOURCES, default=PUBLIC_SOURCES,
                        help=f"default: {' '.join(PUBLIC_SOURCES)} (the entsoe sources need a token)")
    parser.add_argument("--token-file", help="file holding the ENTSO-E API token (one line); else the environment "
                                             "variable ENTSOE_API_KEY is used")
    parser.add_argument("--force", action="store_true", help="re-download files already on disk")
    args = parser.parse_args(argv)
    if args.token_file:
        entsoe.use_token_file(args.token_file)

    steps = {
        "energy_charts": download_energy_charts,
        "swissgrid": download_swissgrid,
        "bfe": lambda s, e, f: bfe.download(f),
        "entsoe": download_entsoe,
        "entsoe_ntc": download_entsoe_ntc,
        "entsoe_units": download_entsoe_units,
    }
    for source in args.sources:
        steps[source](args.start, args.end, args.force)


if __name__ == "__main__":
    main()
