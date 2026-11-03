"""Export the rooftop-PV candidate units of a Nexus-E scenario database for FEM.

Writes, for one run year, every candidate unit of technology PV-roof with its
installable potential and hourly profile into

    input/RES_nexuse/PV/<dataset>/<weather_year>/
        units.csv                         GenName, Pmax, Technology, BusName, idProfile, cost_class,
                                          nexuse_InvCost_EURperMWyr, nexuse_FOM_EURperMWyr
        NexusInput_profiles_newPV_mv.csv  Nexus-E profile table: Name, year, '1'..'8760', ...
        SOURCE.md                         database, server, date and the checks that passed

which is the layout `model/high_res_PV.py` reads (`docs/high_res_pv.md`).

`Pmax` is `genconfiguration.Pmax` of the candidate, the roof potential the
profile was computed for; the profile is `profiledata.timeSeries`, a JSON list
of 8,760 values in MWh per hour. Every profile is checked against its Pmax
before anything is kept: a value below zero or above Pmax stops the export.

`cost_class` is the class FEM prices the unit at when `pv_roof_cost_mode` is
`by_class` (model/pv_roof_costs.py). `--cost-class` says how it is assigned:
`name-suffix` takes the last `_`-separated token of the unit name, which must
be one of COST_CLASS_TOKENS, and stops on any other name; `all` labels every
unit `all`. The two `nexuse_*` columns copy the database's own annualised
costs (EUR per MW and year) for traceability; FEM does not read them.

Usage (the password comes from the environment, never from the command line):

    set NEXUSE_DB_PASSWORD=...
    poetry run python data_prep/export_nexuse_pv.py --db pv_all_eu_evflex_pv09_ewopt \\
        --dataset pv09_ewopt --weather-year 2009 --run-year 2050 --host <server> --user <user> \\
        --cost-class name-suffix

`--units-only` rewrites units.csv (and appends to SOURCE.md) of an exported
folder from the database without touching the profile file; the unit names
must match the profiles already there.

Needs the optional dependency group `nexuse`: `poetry install --with nexuse`.
"""
import argparse
import csv
import datetime as dt
import json
import math
import os
from typing import Dict, Iterable, Iterator, List

import pandas as pd

from model.high_res_PV import PV_DIR, PROFILES_FILE, UNITS_FILE

HOURS_PER_YEAR = 8760
HOUR_COLUMNS = [str(h) for h in range(1, HOURS_PER_YEAR + 1)]
PROFILE_META_COLUMNS = ["Profile Number", "Name", "Country", "year", "type", "resolution", "unit"]
RECORD_COLUMNS = ["GenName", "Pmax", "Technology", "BusName", "idProfile", "InvCost", "FOM_Cost"]
NEXUSE_COST_COLUMNS = {"InvCost": "nexuse_InvCost_EURperMWyr", "FOM_Cost": "nexuse_FOM_EURperMWyr"}
UNIT_COLUMNS = ["GenName", "Pmax", "Technology", "BusName", "idProfile", "cost_class",
                "nexuse_InvCost_EURperMWyr", "nexuse_FOM_EURperMWyr"]
COST_CLASS_TOKENS = ("1-10kW", "10-30kW", "30-100kW", "100-1000kW")
COST_CLASS_ALL = "all"
COST_CLASS_MODES = ("name-suffix", "all")
PV_TECHNOLOGY = "PV-roof"
PASSWORD_ENV = "NEXUSE_DB_PASSWORD"

# Absolute allowance (MW) for a profile value above Pmax. Nexus-E stores the
# profiles rounded to 0.1 kWh, and a few units below 0.04 MW peak up to 0.5 kW
# above their Pmax (5 of 8,556 in the 2009 set); FEM clips these to availability
# 1. Anything larger signals profiles computed for another capacity and stops
# the export.
PMAX_ABS_TOLERANCE_MW = 0.001


def parse_profile(blob: str) -> List[float]:
    """Decode a `profiledata.timeSeries` blob into 8,760 finite floats."""
    try:
        values = json.loads(blob)
    except (TypeError, ValueError) as err:
        raise ValueError(f"profile blob is not JSON: {err}") from err
    if not isinstance(values, list) or len(values) != HOURS_PER_YEAR:
        length = len(values) if isinstance(values, list) else type(values).__name__
        raise ValueError(f"profile must be a list of {HOURS_PER_YEAR} values, got {length}")
    floats = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"profile contains a non-numeric value: {value!r}")
        floats.append(float(value))
    return floats


def check_profile_against_pmax(name: str, values: List[float], pmax: float) -> float:
    """Return how far (MW) the profile peaks above Pmax, 0.0 if it does not.

    Raise if a value is negative or the peak is more than PMAX_ABS_TOLERANCE_MW
    above Pmax.
    """
    lowest, highest = min(values), max(values)
    if lowest < 0:
        raise ValueError(f"{name}: profile has a negative value ({lowest})")
    if highest > pmax + PMAX_ABS_TOLERANCE_MW:
        raise ValueError(f"{name}: profile maximum {highest} exceeds Pmax {pmax}")
    return max(0.0, highest - pmax)


def assign_cost_class(gen_name: str, mode: str) -> str:
    """The cost class of a unit under `mode` (`name-suffix` or `all`); raise on an unknown suffix."""
    if mode == COST_CLASS_ALL:
        return COST_CLASS_ALL
    if mode != "name-suffix":
        raise ValueError(f"cost-class mode must be one of {COST_CLASS_MODES}, got {mode!r}")
    suffix = gen_name.rsplit("_", 1)[-1]
    if suffix not in COST_CLASS_TOKENS:
        raise ValueError(
            f"{gen_name}: last name token {suffix!r} is not a size class {COST_CLASS_TOKENS}; "
            "use --cost-class all for a dataset without size classes"
        )
    return suffix


def build_units_table(records: Iterable[Dict], cost_class_mode: str) -> pd.DataFrame:
    """One row per unit with the columns `units.csv` carries; raise on bad data."""
    units = pd.DataFrame(list(records))
    if units.empty:
        raise ValueError("no candidate units to export")
    missing = [column for column in RECORD_COLUMNS if column not in units.columns]
    if missing:
        raise ValueError(f"records lack column(s) {missing}")
    units["cost_class"] = [assign_cost_class(name, cost_class_mode) for name in units["GenName"]]
    units = units.rename(columns=NEXUSE_COST_COLUMNS)[UNIT_COLUMNS]
    duplicates = units.loc[units["GenName"].duplicated(), "GenName"].tolist()
    if duplicates:
        raise ValueError(f"duplicate unit names: {duplicates[:5]}")
    not_positive = units.loc[units["Pmax"] <= 0, "GenName"].tolist()
    if not_positive:
        raise ValueError(f"units with Pmax <= 0: {not_positive[:5]}")
    return units.reset_index(drop=True)


def _cost_class_line(units: pd.DataFrame, cost_class_mode: str) -> str:
    counts = units["cost_class"].value_counts().to_dict()
    return (f"Cost classes assigned with --cost-class {cost_class_mode}: {counts}; "
            "nexuse_InvCost_EURperMWyr and nexuse_FOM_EURperMWyr copied from genconfiguration "
            "(EUR per MW and year, not read by FEM)")


def export_records(records: Iterable[Dict], out_folder: str, source_lines: List[str],
                   cost_class_mode: str) -> Dict:
    """Write units.csv, the profile table and SOURCE.md from an iterable of records.

    Each record holds the RECORD_COLUMNS, `year` and `timeSeries` (the raw
    blob). Profiles are streamed to the CSV one row at a time, so the full
    table never sits in memory.
    """
    os.makedirs(out_folder, exist_ok=True)
    profiles_path = os.path.join(out_folder, PROFILES_FILE)
    unit_rows: List[Dict] = []
    years: Dict[int, int] = {}
    energy_mwh = 0.0
    units_above_pmax = 0
    max_overshoot_mw = 0.0
    with open(profiles_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(PROFILE_META_COLUMNS + HOUR_COLUMNS)
        for number, record in enumerate(records, start=1):
            values = parse_profile(record["timeSeries"])
            overshoot = check_profile_against_pmax(record["GenName"], values, float(record["Pmax"]))
            if overshoot > 0:
                units_above_pmax += 1
                max_overshoot_mw = max(max_overshoot_mw, overshoot)
            year = int(record["year"])
            years[year] = years.get(year, 0) + 1
            energy_mwh += sum(values)
            writer.writerow(
                [number, record["GenName"], "CH", year, "SolarGen", 1, "MWh"] + values
            )
            unit_rows.append({column: record[column] for column in RECORD_COLUMNS})
    units = build_units_table(unit_rows, cost_class_mode)
    units.to_csv(os.path.join(out_folder, UNITS_FILE), index=False)
    summary = {
        "units": len(units),
        "buses": int(units["BusName"].nunique()),
        "pmax_sum_mw": float(units["Pmax"].sum()),
        "energy_twh_at_pmax": energy_mwh / 1e6,
        "profile_years": years,
        "units_above_pmax": units_above_pmax,
        "max_overshoot_mw": max_overshoot_mw,
    }
    with open(os.path.join(out_folder, "SOURCE.md"), "w", encoding="utf-8") as handle:
        handle.write("# Source of this dataset\n\n")
        for line in source_lines:
            handle.write(f"- {line}\n")
        handle.write(f"- Units: {summary['units']} on {summary['buses']} buses, "
                     f"sum Pmax = {summary['pmax_sum_mw']:.1f} MW, "
                     f"energy at Pmax = {summary['energy_twh_at_pmax']:.2f} TWh, "
                     f"profile years = {summary['profile_years']}\n")
        handle.write("- Checks passed: 8760 values per profile, none negative, none more than "
                     f"{PMAX_ABS_TOLERANCE_MW * 1000:.0f} kW above Pmax, unique names, Pmax > 0, "
                     "one profile per unit\n")
        handle.write(f"- Units whose profile peaks above Pmax (FEM clips them to availability 1): "
                     f"{units_above_pmax}, largest excess {max_overshoot_mw * 1000:.2f} kW\n")
        handle.write(f"- {_cost_class_line(units, cost_class_mode)}\n")
    return summary


def export_units_only(records: Iterable[Dict], out_folder: str, source_lines: List[str],
                      cost_class_mode: str) -> Dict:
    """Rewrite units.csv of an exported folder and append to its SOURCE.md; profiles stay as they are.

    The unit names must be exactly the names of the profile file already in
    `out_folder`, so the two files keep describing the same units.
    """
    profiles_path = os.path.join(out_folder, PROFILES_FILE)
    if not os.path.isfile(profiles_path):
        raise FileNotFoundError(f"{profiles_path} not found: export the dataset in full first")
    units = build_units_table(({column: record[column] for column in RECORD_COLUMNS} for record in records),
                              cost_class_mode)
    profile_names = set(pd.read_csv(profiles_path, usecols=["Name"])["Name"])
    unit_names = set(units["GenName"])
    if unit_names != profile_names:
        raise ValueError(
            f"units do not match the profiles in {out_folder}: "
            f"{len(unit_names - profile_names)} unit(s) without profile "
            f"(e.g. {sorted(unit_names - profile_names)[:3]}), "
            f"{len(profile_names - unit_names)} profile(s) without unit "
            f"(e.g. {sorted(profile_names - unit_names)[:3]})"
        )
    units.to_csv(os.path.join(out_folder, UNITS_FILE), index=False)
    with open(os.path.join(out_folder, "SOURCE.md"), "a", encoding="utf-8") as handle:
        for line in source_lines:
            handle.write(f"- {line}\n")
        handle.write(f"- {_cost_class_line(units, cost_class_mode)}\n")
    summary = {"units": len(units), "buses": int(units["BusName"].nunique()),
               "pmax_sum_mw": float(units["Pmax"].sum()),
               "cost_classes": units["cost_class"].value_counts().to_dict()}
    return summary


def _gen_config_id(cursor, run_year: int) -> int:
    cursor.execute("SELECT idGenConfig FROM genconfiginfo WHERE year = %s", (run_year,))
    ids = [row[0] for row in cursor.fetchall()]
    if len(ids) != 1:
        raise ValueError(f"expected one generator configuration for year {run_year}, found {ids}")
    return int(ids[0])


# One batch of candidates after generator id %s, in id order. Batches rather
# than one streamed result set: the server closes a result set the client has
# not drained for 60 seconds, and parsing 8,556 profiles takes longer.
CANDIDATE_BATCH_QUERY = """
SELECT gc.idGen, g.GenName, gc.Pmax, g.Technology, b.BusName, gc.idProfile, gc.InvCost, gc.FOM_Cost,
       p.year, p.timeSeries
FROM genconfiguration gc
JOIN gendata g ON g.idGen = gc.idGen
LEFT JOIN busdata b ON b.idBus = gc.idBus
LEFT JOIN profiledata p ON p.idProfile = gc.idProfile
WHERE gc.idGenConfig = %s AND g.Technology = %s AND gc.CandidateUnit = 1 AND gc.idGen > %s
ORDER BY gc.idGen
LIMIT %s
"""

# The same units without their profiles, for --units-only.
UNITS_BATCH_QUERY = """
SELECT gc.idGen, g.GenName, gc.Pmax, g.Technology, b.BusName, gc.idProfile, gc.InvCost, gc.FOM_Cost
FROM genconfiguration gc
JOIN gendata g ON g.idGen = gc.idGen
LEFT JOIN busdata b ON b.idBus = gc.idBus
WHERE gc.idGenConfig = %s AND g.Technology = %s AND gc.CandidateUnit = 1 AND gc.idGen > %s
ORDER BY gc.idGen
LIMIT %s
"""

BATCH_SIZE = 200


def iterate_in_batches(fetch_batch, batch_size: int = BATCH_SIZE) -> Iterator[Dict]:
    """Yield records from `fetch_batch(last_id, batch_size)` until a batch comes back short.

    `fetch_batch` returns the records with `idGen` greater than `last_id`, in
    id order, at most `batch_size` of them; each record must carry `idGen`.
    """
    last_id = 0
    while True:
        batch = fetch_batch(last_id, batch_size)
        for record in batch:
            yield record
        if len(batch) < batch_size:
            return
        last_id = batch[-1]["idGen"]


def _fetch_rows(connection, run_year: int, query: str) -> Iterator[Dict]:
    """Yield the rows of `query` for the generator configuration of `run_year`, in batches."""
    import pymysql.cursors  # optional dependency, group `nexuse`

    with connection.cursor() as cursor:
        config_id = _gen_config_id(cursor, run_year)

    def fetch_batch(last_id, batch_size):
        with connection.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute(query, (config_id, PV_TECHNOLOGY, last_id, batch_size))
            return cursor.fetchall()

    return iterate_in_batches(fetch_batch)


def fetch_candidates(connection, run_year: int) -> Iterator[Dict]:
    """Yield one record per PV-roof candidate of `run_year` with its profile, read in batches."""
    for row in _fetch_rows(connection, run_year, CANDIDATE_BATCH_QUERY):
        if row["timeSeries"] is None or row["year"] is None:
            raise ValueError(f"{row['GenName']}: no profile for idProfile={row['idProfile']}")
        yield row


def fetch_units(connection, run_year: int) -> Iterator[Dict]:
    """Yield one record per PV-roof candidate of `run_year` without its profile."""
    return _fetch_rows(connection, run_year, UNITS_BATCH_QUERY)


def export_database(db: str, dataset: str, weather_year: int, run_year: int,
                    host: str, port: int, user: str, password: str, cost_class_mode: str,
                    units_only: bool = False, out_root: str = PV_DIR) -> Dict:
    """Connect, stream the candidates of `db` and write the dataset folder (or only its unit table)."""
    try:
        import pymysql
    except ImportError as err:
        raise ImportError("pymysql is missing: run `poetry install --with nexuse`") from err
    out_folder = os.path.join(out_root, dataset, str(weather_year))
    source_lines = [
        f"Database `{db}` on {host}:{port}, generator configuration of run year {run_year}, "
        f"candidate units of technology {PV_TECHNOLOGY}",
        f"Exported {dt.date.today().isoformat()} by data_prep/export_nexuse_pv.py",
        f"Weather year {weather_year} as given on the command line (the database does not record it)",
        "Pmax = genconfiguration.Pmax (installable potential, MW); "
        "profiles = profiledata.timeSeries (MWh per hour at Pmax)",
    ]
    connection = pymysql.connect(host=host, port=port, user=user, password=password, database=db)
    try:
        if units_only:
            source_lines = [f"Unit table rewritten {dt.date.today().isoformat()} from `{db}` on {host}:{port} "
                            "by data_prep/export_nexuse_pv.py --units-only; profiles untouched"]
            summary = export_units_only(fetch_units(connection, run_year), out_folder, source_lines,
                                        cost_class_mode)
        else:
            summary = export_records(fetch_candidates(connection, run_year), out_folder, source_lines,
                                     cost_class_mode)
    finally:
        connection.close()
    print(f"[export_nexuse_pv] wrote {out_folder}: {summary}")
    return summary


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--db", required=True, help="Nexus-E scenario database to read")
    parser.add_argument("--dataset", required=True, help="folder name under input/RES_nexuse/PV/")
    parser.add_argument("--weather-year", type=int, required=True,
                        help="meteorological year of the profiles")
    parser.add_argument("--run-year", type=int, required=True,
                        help="run year whose generator configuration is read")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", required=True)
    parser.add_argument("--cost-class", required=True, choices=COST_CLASS_MODES,
                        help="how units.csv gets its cost_class: from the unit name's last token, or 'all'")
    parser.add_argument("--units-only", action="store_true",
                        help="rewrite units.csv and append to SOURCE.md of an exported folder; keep the profiles")
    args = parser.parse_args(argv)
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        raise SystemExit(f"set the environment variable {PASSWORD_ENV} to the database password")
    export_database(args.db, args.dataset, args.weather_year, args.run_year,
                    args.host, args.port, args.user, password,
                    cost_class_mode=args.cost_class, units_only=args.units_only)


if __name__ == "__main__":
    main()
