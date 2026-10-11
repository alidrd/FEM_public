"""Copernicus PECD 4.2 hydro inflow for CH00: register a CDS archive downloaded by hand, and build the weekly
table of reservoir (HRI) and open-loop pumped-storage (HOL) inflow that process.py writes.

Register each archive once (from the repo root):
    python -m data_prep.historical.pecd register <archive.zip>
It is checked, copied to raw/pecd/ and recorded in raw_manifest.json; process.py then builds
pecd_inflow_weekly_ch00.csv from every registered archive.

Conventions, from the C3S product user guide (checked 2026-10-11) and the files themselves:
- values are electrical energy at grid level, MWh per week, aggregated Monday to Sunday;
- each date is the Monday that starts its week; the time zone is undocumented, and 00:00 UTC is inferred;
- PECD already applies its CH00 correction factors (HOL x0.825, run of river x1.39, HRI none);
- interim (ITE) monthly files are model estimates, revised into annual (COM) files after year end.
"""
import argparse
import datetime
import hashlib
import io
import re
import shutil
import zipfile
from pathlib import Path

import pandas as pd

from data_prep.historical import http_get
from data_prep.historical.config import PECD_SERIES, PECD_ZONE, RAW_DIR

RAW = RAW_DIR / "pecd"
TABLE = "pecd_inflow_weekly_ch00.csv"
MEMBER = re.compile(
    r"H_ERA5_ECMW_T639_(?P<series>[A-Z]{3})_0000m_Pecd_SZON_S(?P<first>\d{12})_E(?P<last>\d{12})"
    r"_NRG_TIM_(?P<resolution>[0-9a-z]+)_(?P<release>COM|ITE)_.*_(?P<version>fv\d+)\.csv"
)
WEEK_CONVENTION = ("each date is the Monday that starts a 7-day week at 00:00 UTC; inferred from the file layout, "
                   "since the C3S documentation does not state the time zone")
CORRECTION_FACTORS = ("PECD 4.2 already applies its CH00 correction factors (HOL x0.825, run of river x1.39, "
                      "HRI none); they must not be applied again")
ESTIMATES = ("ERA5-driven model estimates (Random Forest trained on data up to 2022); interim (ITE) monthly files "
             "are revised into annual (COM) files after year end")


def check_member_name(name: str) -> dict:
    """Parts of an archive member's name; raises on an unsafe path or a name outside the PECD pattern."""
    if "/" in name or "\\" in name or ".." in name or ":" in name:
        raise ValueError(f"unsafe path inside the PECD archive: {name!r}")
    match = MEMBER.fullmatch(name)
    if not match:
        raise ValueError(f"unexpected file in the PECD archive: {name!r}")
    return match.groupdict()


def read_member(name: str, text: str) -> tuple[pd.DataFrame, dict]:
    """Weekly CH00 energy of one archive member and its metadata; raises on any deviation from the format."""
    parts = check_member_name(name)
    lines = text.splitlines()
    meta = {key[3:].strip(): value[4:].strip()
            for key, value in zip(lines, lines[1:]) if key.startswith("## ") and value.startswith("### ")}
    if meta.get("Unit") != "MWh":
        raise ValueError(f"{name}: unit {meta.get('Unit')!r}, expected 'MWh'")
    if parts["resolution"] != "07d" or meta.get("Temporal resolution") != "7 days":
        raise ValueError(f"{name}: resolution {parts['resolution']!r} / {meta.get('Temporal resolution')!r}, "
                         f"expected 7 days")
    data_start = next(number for number, line in enumerate(lines) if not line.startswith("#"))
    frame = pd.read_csv(io.StringIO("\n".join(lines[data_start:])))
    if PECD_ZONE not in frame.columns:
        raise ValueError(f"{name}: no {PECD_ZONE} column")
    if frame.empty:
        raise ValueError(f"{name}: no weeks")
    weeks = pd.to_datetime(frame["Date"], format="%Y-%m-%d")
    not_monday = weeks[weeks.dt.dayofweek != 0]
    if len(not_monday):
        raise ValueError(f"{name}: dates that are not a Monday, e.g. {not_monday.iloc[0].date()}")
    if (weeks.diff().dropna() != pd.Timedelta(days=7)).any():
        raise ValueError(f"{name}: dates are not 7 days apart")
    named = (pd.Timestamp(parts["first"][:8]), pd.Timestamp(parts["last"][:8]))
    if (weeks.iloc[0], weeks.iloc[-1]) != named:
        raise ValueError(f"{name}: dates run {weeks.iloc[0].date()} to {weeks.iloc[-1].date()}, "
                         f"the name says {named[0].date()} to {named[1].date()}")
    values = pd.to_numeric(frame[PECD_ZONE], errors="coerce")
    bad = values.isna() | values.isin([float("inf"), float("-inf")]) | (values < 0)
    if bad.any():
        raise ValueError(f"{name}: {int(bad.sum())} {PECD_ZONE} values are missing, non-finite or negative, "
                         f"e.g. the week of {weeks[bad].iloc[0].date()}")
    return pd.DataFrame({"week_start_utc": weeks.dt.tz_localize("UTC"), "energy_MWh": values.to_numpy()}), meta


def register(source: Path) -> str:
    """Check a CDS archive, copy it to raw/pecd/ and record it in raw_manifest.json; returns its manifest key."""
    source = Path(source)
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if check_member_name(name)["series"] in PECD_SERIES:
                read_member(name, archive.read(name).decode("utf-8"))
    RAW.mkdir(parents=True, exist_ok=True)
    target = RAW / source.name
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    key = http_get.manifest_key(target)
    manifest = http_get.load_manifest()
    manifest[key] = {
        "url": "manual request on the Copernicus CDS, dataset sis-energy-pecd",
        "retrieved_at_utc": None,
        "registered_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "bytes": target.stat().st_size,
        "sha256": http_get.sha256(target),
    }
    http_get.save_manifest(manifest)
    return key


def registered_archives() -> list[Path]:
    """The registered archives in raw/pecd/; raises on an unregistered, missing or changed archive."""
    registered = {key: entry for key, entry in http_get.load_manifest().items() if key.startswith("pecd/")}
    on_disk = {http_get.manifest_key(path): path for path in sorted(RAW.glob("*.zip"))} if RAW.exists() else {}
    unregistered = sorted(set(on_disk) - set(registered))
    if unregistered:
        raise ValueError(f"PECD archives not registered: {unregistered}; "
                         f"run python -m data_prep.historical.pecd register <archive>")
    missing = sorted(set(registered) - set(on_disk))
    if missing:
        raise FileNotFoundError(f"registered PECD archives missing from {RAW}: {missing}; register them again or "
                                f"remove their entries from {http_get.MANIFEST.name}")
    for key, path in on_disk.items():
        if http_get.sha256(path) != registered[key]["sha256"]:
            raise RuntimeError(f"{key} changed since it was registered; register it again")
    return [on_disk[key] for key in sorted(on_disk)]


def build_weekly_inflow(archives: list[Path], start_utc: pd.Timestamp, end_utc: pd.Timestamp) -> tuple[pd.DataFrame, dict]:
    """Weekly CH00 inflow per series for every week overlapping [start_utc, end_utc), and its provenance.

    Identical values for one week from several files are merged, preferring the annual file and then the
    latest publication; conflicting values raise. A week no archive contains keeps no value.
    """
    pieces, members = [], []
    for path in archives:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                parts = check_member_name(name)
                content = archive.read(name)
                members.append({"archive": path.name, "member": name, "sha256": hashlib.sha256(content).hexdigest(),
                                "series": parts["series"], "release": parts["release"], "file_version": parts["version"]})
                if parts["series"] not in PECD_SERIES:
                    continue
                weekly, meta = read_member(name, content.decode("utf-8"))
                members[-1]["published"] = meta.get("Date")
                pieces.append(weekly.assign(series=parts["series"], release=parts["release"],
                                            file_version=parts["version"], published=meta.get("Date"),
                                            source_file=name, archive=path.name))
    found = pd.concat(pieces, ignore_index=True)
    distinct = found.groupby(["series", "week_start_utc"])["energy_MWh"].nunique()
    if (distinct > 1).any():
        series, week = distinct[distinct > 1].index[0]
        clash = found[(found["series"] == series) & (found["week_start_utc"] == week)]
        raise ValueError(f"conflicting PECD values for {series} in the week of {week.date()}: "
                         f"{dict(zip(clash['source_file'], clash['energy_MWh']))}")
    found["annual"] = found["release"] == "COM"
    found = (found.sort_values(["series", "week_start_utc", "annual", "published"], ascending=[True, True, False, False])
             .drop_duplicates(["series", "week_start_utc"]).drop(columns="annual"))

    first_week = start_utc.normalize() - pd.Timedelta(days=start_utc.dayofweek)
    weeks = pd.date_range(first_week, end_utc, freq="7D", inclusive="left")
    grid = pd.MultiIndex.from_product([list(PECD_SERIES), weeks], names=["series", "week_start_utc"]).to_frame(index=False)
    table = grid.merge(found, on=["series", "week_start_utc"], how="left")
    table.insert(1, "week_end_utc", table["week_start_utc"] + pd.Timedelta(days=7))
    lacking = table[table["energy_MWh"].isna()]
    record = {
        "archives": [{"file": http_get.manifest_key(path), "sha256": http_get.sha256(path)} for path in archives],
        "members": members,
        "zone": PECD_ZONE,
        "series_used": list(PECD_SERIES),
        "week_convention": WEEK_CONVENTION,
        "correction_factors": CORRECTION_FACTORS,
        "values": ESTIMATES,
        "weeks_without_value": {series: [w.date().isoformat() for w in group["week_start_utc"]]
                                for series, group in lacking.groupby("series")},
    }
    return table, record


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    register_command = commands.add_parser("register", help="check a CDS archive, copy it to raw/pecd/ and record it")
    register_command.add_argument("archive", type=Path)
    args = parser.parse_args(argv)
    key = register(args.archive)
    print(f"registered {key} in {http_get.MANIFEST.name}; run process.py to build {TABLE}")


if __name__ == "__main__":
    main()
