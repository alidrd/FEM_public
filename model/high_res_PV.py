"""Loader for per-plant rooftop-PV data exported from Nexus-E.

Single source of truth for where the data comes from. Both
read_RES_avail_data() and read_plant_non_hydro_data() call this helper
from inside an `if high_resolution_PV:` branch at the existing
PV read sites.

Layout, one folder per dataset and weather year:

    input/RES_nexuse/PV/<dataset>/<weather_year>/
        units.csv                         GenName, Pmax (MW, installable potential), Technology,
                                          cost_class (optional, see model/pv_roof_costs.py)
        NexusInput_profiles_newPV_mv.csv  Name, year, '1'..'8760' (MWh per hour at Pmax)

`data_prep/export_nexuse_pv.py` writes both files from a Nexus-E scenario
database; the scenario setting `high_resolution_PV_dataset` names the folder.
"""
import os
import functools
import numpy as np
import pandas as pd

PV_DIR = os.path.join("input", "RES_nexuse", "PV")
UNITS_FILE = "units.csv"
PROFILES_FILE = "NexusInput_profiles_newPV_mv.csv"
UNIT_COLUMNS = ("GenName", "Pmax", "Technology")
COST_CLASS_COLUMN = "cost_class"
PV_ROOF_TECHNOLOGY = "PV-roof"
AGGREGATED_PV_PATTERN = r"^CH0[1-7]_pvrf$"   # the seven regional candidates the per-plant ones replace

# Hourly columns of the Nexus-E profile CSV and the FEM time labels they map to.
HOUR_COLS = [str(h) for h in range(1, 8761)]
T_LABELS = [f"t_{h}" for h in range(1, 8761)]


def _sanitize(name: str) -> str:
    return name.replace("-", "_")


def plant_id(gen_name: str) -> str:
    return f"CH00_{_sanitize(gen_name)}_pvrf"


def _subfolders(folder):
    """Sorted names of the sub-folders of `folder`, or [] when it is not a folder."""
    if not os.path.isdir(folder):
        return []
    return sorted(d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d)))


def _resolve_data_folder(dataset, weather_year):
    """Return the folder for `dataset` and `weather_year`, or raise if either is missing."""
    if dataset is None or str(dataset) == "":
        raise ValueError(
            "[high_res_PV] high_resolution_PV is on but the setting "
            "high_resolution_PV_dataset is not set. "
            f"Available datasets under {PV_DIR}: {_subfolders(PV_DIR)}."
        )
    dataset_dir = os.path.join(PV_DIR, str(dataset))
    if not os.path.isdir(dataset_dir):
        raise FileNotFoundError(
            f"[high_res_PV] No dataset folder {str(dataset)!r} under {PV_DIR}. "
            f"Available: {_subfolders(PV_DIR)}. Either export the dataset "
            f"(data_prep/export_nexuse_pv.py) or turn high_resolution_PV off."
        )
    folder = os.path.join(dataset_dir, str(weather_year))
    if not os.path.isdir(folder):
        raise FileNotFoundError(
            f"[high_res_PV] No data folder for weather_year={weather_year} "
            f"under {dataset_dir}. Available: {_subfolders(dataset_dir)}. "
            f"Either provide the data or turn high_resolution_PV off."
        )
    return folder


def _read_units(folder):
    """Read units.csv of `folder`; raise if the file or a required column is missing."""
    path = os.path.join(folder, UNITS_FILE)
    if not os.path.isfile(path):
        raise FileNotFoundError(f"[high_res_PV] {path} not found.")
    units = pd.read_csv(path)
    missing = [c for c in UNIT_COLUMNS if c not in units.columns]
    if missing:
        raise ValueError(
            f"[high_res_PV] {path} lacks column(s) {missing}; expected {list(UNIT_COLUMNS)}."
        )
    return units


def _pv_roof_units(folder):
    """The rows of units.csv that become candidates: technology PV-roof with a positive Pmax."""
    units = _read_units(folder)
    return units[(units["Technology"] == PV_ROOF_TECHNOLOGY) & (units["Pmax"] > 0)].copy()


@functools.lru_cache(maxsize=8)
def load_cost_classes(dataset, weather_year):
    """{plant_id: cost class} of the PV-roof units, or None when units.csv has no `cost_class` column.

    A PV-roof unit with an empty class is an error: the column must then be
    rewritten with data_prep/export_nexuse_pv.py --units-only --cost-class ...
    """
    folder = _resolve_data_folder(dataset, weather_year)
    units = _pv_roof_units(folder)
    if COST_CLASS_COLUMN not in units.columns:
        return None
    classes = units[COST_CLASS_COLUMN].astype("string").str.strip()
    blank = units.loc[classes.isna() | (classes == ""), "GenName"].tolist()
    if blank:
        raise ValueError(
            f"[high_res_PV] {len(blank)} PV-roof unit(s) of dataset {dataset!r} have an empty "
            f"{COST_CLASS_COLUMN} in {UNITS_FILE} (e.g. {blank[:3]})."
        )
    return {plant_id(name): str(cls) for name, cls in zip(units["GenName"], classes)}


def replace_aggregated_pv_candidates(candidates, per_plant_candidates):
    """Drop the aggregated rows `CH01_pvrf`..`CH07_pvrf` from `candidates`, append the per-plant ones.

    Returns the new frame and the number of rows dropped. With the flag on the
    aggregated candidates must leave the investment set: the pre-existing
    capacity they stood for stays in the model as fixed infeed.
    """
    aggregated = pd.Series(candidates.index.str.match(AGGREGATED_PV_PATTERN), index=candidates.index)
    mask = candidates["tech"].eq("pvrf") & aggregated
    return pd.concat([candidates[~mask], per_plant_candidates]), int(mask.sum())


def _resolve_run_year(profiles: pd.DataFrame, run_year):
    """Return the closest available profile-year to `run_year`.

    If `run_year` is present in the CSV, return it unchanged. Otherwise pick
    the year minimising absolute difference (ties broken by smaller year) and
    print a substitution-notice log line.
    """
    available = sorted(int(y) for y in profiles["year"].unique())
    if int(run_year) in available:
        return int(run_year)
    closest = min(available, key=lambda y: (abs(y - int(run_year)), y))
    print(
        f"[high_res_PV] requested run_year={run_year} not present in profile CSV; "
        f"using closest available year={closest} (available: {available})"
    )
    return closest


@functools.lru_cache(maxsize=8)
def load_high_res_PV(dataset, weather_year, run_year):
    """
    Load per-plant PV data of one dataset for the given weather and run year.

    Returns
    -------
    plant_records : pd.DataFrame
        Indexed by plant_id, columns matching plants_invest_candidates_res_CH.csv
        schema (node, market, plant_type, tech, upperwn, lowerwn, eta, eta_pump,
        n_redispatch, gen_max_limit, energy_max_limit, fuel_switching).
    avail : dict
        {(plant_id, "t_<h>"): availability_factor in [0, 1]}
    """
    folder = _resolve_data_folder(dataset, weather_year)

    # --- units --------------------------------------------------------------
    units = _pv_roof_units(folder)[["GenName", "Pmax"]]
    units["plant_id"] = units["GenName"].map(plant_id)

    # --- profiles -----------------------------------------------------------
    # Only the join key, the profile year and the 8760 hourly columns are read;
    # the descriptive columns of the Nexus-E export are skipped at parse time.
    profiles = pd.read_csv(
        os.path.join(folder, PROFILES_FILE),
        usecols=["Name", "year"] + HOUR_COLS,
    )
    resolved_year = _resolve_run_year(profiles, run_year)
    profiles = profiles[profiles["year"] == resolved_year]

    # --- inner-join ---------------------------------------------------------
    merged = units.merge(profiles, left_on="GenName", right_on="Name", how="inner")
    dropped_units = len(units) - len(merged)
    dropped_profiles = len(profiles) - len(merged)
    if dropped_units or dropped_profiles:
        print(
            f"[high_res_PV] dropped {dropped_units} unit(s) without profile, "
            f"{dropped_profiles} profile(s) without unit"
        )

    # --- scale to availability factor ---------------------------------------
    af = merged[HOUR_COLS].to_numpy(dtype=float) / merged["Pmax"].to_numpy(dtype=float)[:, None]
    np.clip(af, 0.0, 1.0, out=af)

    # --- build avail dict ---------------------------------------------------
    # One bulk update per plant. The hour labels are shared objects and no
    # per-element DataFrame lookup is made: with 4,535 plants the dict has
    # about 40 million entries, and element-wise `.at` access took minutes.
    avail = {}
    for pid, row in zip(merged["plant_id"], af):
        avail.update(zip(((pid, t) for t in T_LABELS), row.tolist()))

    # --- build plant_records ------------------------------------------------
    n = len(merged)
    plant_records = pd.DataFrame({
        "node":             ["CH00"] * n,
        "market":           ["CH00"] * n,
        "plant_type":       ["RES"] * n,
        "tech":             ["pvrf"] * n,
        "upperwn":          [float("nan")] * n,
        "lowerwn":          [float("nan")] * n,
        "eta":              [float("nan")] * n,
        "eta_pump":         [float("nan")] * n,
        "n_redispatch":     ["CH00"] * n,
        "gen_max_limit":    merged["Pmax"].astype(float).values,
        "energy_max_limit": [float("inf")] * n,
        "fuel_switching":   [float("nan")] * n,
    }, index=merged["plant_id"].values)
    plant_records.index.name = "index"

    total_pmax_gw = merged["Pmax"].sum() / 1000.0
    print(
        f"[high_res_PV] loaded {len(merged)} PV-roof units, "
        f"sum Pmax = {total_pmax_gw:.2f} GW "
        f"(dataset={dataset}, weather_year={weather_year}, profile_year={resolved_year})"
    )

    return plant_records, avail
