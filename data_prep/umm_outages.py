"""Plant outages for a back-cast from EEX urgent market messages (UMMs) of Swiss storage-hydro units.

Usage: python -I data_prep/umm_outages.py <first day> <last day> <output CSV> <EEX JSON file> [...]
       (days are local dates of the period, both included; the JSON files are the EEX API's
        non-availability-events/Power?country=CH responses, saved in input/historical/umm_eex/)

The output is an outage table for the setting historical_hydro_outages (read_outages in historical_import.py): per
fleet plant, UTC windows with the turbine and pump MW still available. Messages are read as EEX publishes them (see
read_messages); units are mapped to WASTA plants by input/historical/umm_unit_map.csv, and every unit with a message
must be in that map, either with its plants or with the reason it has none.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

UNIT_MAP = "input/historical/umm_unit_map.csv"
FUELS = ("Hydro Water Reservoir", "Hydro Pumped Storage")  # run-of-river is observed generation in a back-cast
OUTAGE_COLUMNS = ["plant", "start_utc", "end_utc", "turbine_MW", "pump_MW", "source"]
TIME_FORMAT = "%Y-%m-%d %H:%M"
MW_DECIMALS = 1  # remaining capacity is rounded to 0.1 MW, so that equal hours merge into one window


def read_messages(paths):
    """One row per message: the newest version of each message (messageID '<base>_<version>'), kept if it is active
    production unavailability of a storage-hydro unit, so an event whose newest version is dismissed is dropped.
    Columns eic, unit, start, stop (UTC), unavailable_MW, installed_MW. Raises on a capacity profile within an event
    (capacityIntervals), which this does not read."""
    frames = []
    for path in paths:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        frames.append(pd.DataFrame(raw["data"], columns=raw["header"]))
    table = pd.concat(frames, ignore_index=True)  # a message in two files is one version: one survives below
    split = table["messageID"].str.rsplit("_", n=1)
    table = table.assign(base=split.str[0], version=split.str[1].astype(int))
    newest = table.sort_values("version").groupby("base").tail(1)
    kept = newest[(newest["eventStatus"] == "Active") & (newest["eventType"] == "Production unavailability")
                  & newest["fuelType"].isin(FUELS)]
    profiles = kept[kept["capacityIntervals"].map(bool)]
    if len(profiles):
        raise ValueError(f"messages {list(profiles['messageID'])[:3]} carry a capacity profile, which is not read")
    return pd.DataFrame({
        "eic": kept["eIC"].to_numpy(), "unit": (kept["facilityName"] + " / " + kept["unitName"]).to_numpy(),
        "start": pd.to_datetime(kept["eventStart"], utc=True).to_numpy(),
        "stop": pd.to_datetime(kept["eventStop"], utc=True).to_numpy(),
        "unavailable_MW": kept["nonAvailableCapacity"].astype(float).to_numpy(),
        "installed_MW": kept["installedCapacity"].astype(float).to_numpy()}).assign(
        start=lambda f: pd.to_datetime(f["start"], utc=True), stop=lambda f: pd.to_datetime(f["stop"], utc=True))


def unit_unavailable(messages, hours):
    """Unavailable MW of each unit in each UTC hour: every message's MW times its overlap with the hour, summed over
    the unit's concurrent messages (one message is often one machine) and capped at the unit's installed MW."""
    starts = hours.tz_convert("UTC").tz_localize(None).to_numpy()  # naive UTC, for numpy arithmetic
    ends = starts + np.timedelta64(1, "h")
    hour = np.timedelta64(1, "h")
    result = {}
    for eic, group in messages.groupby("eic"):
        total = np.zeros(len(hours))
        naive = {c: group[c].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy() for c in ("start", "stop")}
        for start, stop, mw in zip(naive["start"], naive["stop"], group["unavailable_MW"]):
            overlap = (np.minimum(ends, stop) - np.maximum(starts, start)) / hour
            total += mw * np.clip(overlap, 0.0, 1.0)
        result[eic] = np.minimum(total, group["installed_MW"].max())
    return pd.DataFrame(result, index=hours)


def plant_unavailable(unit_mw, unit_map, gen_max, installed):
    """Unavailable MW of each fleet plant per hour, and the units left out with the map's reason. A unit mapped to one
    plant passes its MW on; a unit mapped to several plants gives each plant capacity / unit installed of them, so a
    full outage takes all of them out. Raises on a unit missing from the map and on a mapped plant outside the
    fleet."""
    mapping = unit_map.set_index("eic")
    missing = sorted(set(unit_mw.columns) - set(mapping.index))
    if missing:
        raise ValueError(f"units {missing} are not in the unit map {UNIT_MAP}")
    plants, left_out = {}, {}
    for eic in unit_mw.columns:
        listed = mapping.at[eic, "plants"]
        names = [name for name in str(listed).split(";") if name] if isinstance(listed, str) else []
        if not names:
            left_out[eic] = mapping.at[eic, "note"]
            continue
        outside = [name for name in names if name not in gen_max]
        if outside:
            raise ValueError(f"{UNIT_MAP}: plants {outside} of unit {eic} are not in the fleet")
        for name in names:
            share = 1.0 if len(names) == 1 else gen_max[name] / installed[eic]
            plants[name] = plants.get(name, 0.0) + unit_mw[eic].to_numpy() * share
    return pd.DataFrame(plants, index=unit_mw.index), left_out


def availability_windows(plant_mw, gen_max, pump_max, sources):
    """Rows of the outage table: for every plant, each run of consecutive hours with the same remaining capacity
    below the installed one, its UTC window and the turbine and pump MW still available (installed minus unavailable,
    at least 0, rounded to 0.1 MW; pumps scaled by the same fraction as the turbines)."""
    rows = []
    for plant in plant_mw.columns:
        remaining = np.round(np.clip(gen_max[plant] - plant_mw[plant].to_numpy(), 0.0, gen_max[plant]), MW_DECIMALS)
        pumps = np.round(pump_max[plant] * remaining / gen_max[plant], MW_DECIMALS)
        out = remaining < round(gen_max[plant], MW_DECIMALS)
        hours = plant_mw.index
        i = 0
        while i < len(hours):
            if not out[i]:
                i += 1
                continue
            j = i
            while j + 1 < len(hours) and out[j + 1] and remaining[j + 1] == remaining[i]:
                j += 1
            rows.append([plant, hours[i].strftime(TIME_FORMAT), (hours[j] + pd.Timedelta(hours=1)).strftime(TIME_FORMAT),
                         float(remaining[i]), float(pumps[i]), sources[plant]])
            i = j + 1
    return pd.DataFrame(rows, columns=OUTAGE_COLUMNS)


def main(argv):
    from data_prep import historical_import as hi

    first, last, output, *paths = argv
    start, end = hi.period_bounds_utc(f"{first}/{last}")
    hours = pd.date_range(start, end, freq="h", inclusive="left")
    messages = read_messages(paths)
    unit_mw = unit_unavailable(messages, hours)
    installed = messages.groupby("eic")["installed_MW"].max().to_dict()
    unit_map = pd.read_csv(UNIT_MAP, dtype=str, keep_default_na=False)
    plants, _, _, gen_max, _, pumping, _, _ = hi.wasta_fleet()
    pump_max = {p: pumping.get(p, 0.0) for p in plants}
    plant_mw, left_out = plant_unavailable(unit_mw, unit_map, gen_max, installed)
    mapping = unit_map.set_index("eic")
    sources = {p: "EEX UMM " + ", ".join(e for e in unit_mw.columns if p in str(mapping.at[e, "plants"]).split(";"))
               for p in plant_mw.columns}
    windows = availability_windows(plant_mw, gen_max, pump_max, sources)
    windows.to_csv(output, index=False)
    print(f"{len(messages)} messages of {messages['eic'].nunique()} units; {len(windows)} windows for "
          f"{windows['plant'].nunique()} plants written to {output}")
    print(f"unavailable storage hydro, mean over the period: units {unit_mw.sum(axis=1).mean():,.0f} MW, fleet plants "
          f"{plant_mw.sum(axis=1).mean():,.0f} MW")
    for eic, reason in left_out.items():
        print(f"left out {eic} ({messages.loc[messages['eic'] == eic, 'unit'].iloc[0]}), mean "
              f"{unit_mw[eic].mean():,.0f} MW: {reason}")


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    main(sys.argv[1:])
