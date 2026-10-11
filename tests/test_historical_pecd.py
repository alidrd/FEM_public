"""Unit tests for the PECD inflow in the historical pipeline (data_prep/historical/pecd.py and its use in
process.py). The tests write small CDS archives in the PECD 4.2 file format into a temporary raw folder.
"""
import json
import math
import zipfile

import pandas as pd
import pytest

from data_prep.historical import config, http_get, pecd, process, provenance
from data_prep.historical.timeutils import period_bounds_utc

# Mondays; 2025-10-26 (Sunday) is the end of daylight saving time in Europe
WEEKS = ["2025-09-22", "2025-09-29", "2025-10-06", "2025-10-13", "2025-10-20"]
START_UTC, END_UTC = period_bounds_utc("2025-10-01", "2025-10-14")  # overlaps the weeks of 09-29, 10-06, 10-13


def member_text(weeks, values, unit="MWh", resolution="7 days", zone="CH00", published="2025-06-17"):
    header = [
        "# General", "## Title", "### Hydropower Inflow; data for ENTSO-E PECD Version 4.2; file version fv1",
        "## Date", f"### {published}", "## Unit", f"### {unit}", "## Temporal resolution", f"### {resolution}", "#",
    ]
    rows = [f"Date,AT00,{zone}"] + [f"{week},1.0,{value}" for week, value in zip(weeks, values)]
    return "\n".join(header + rows) + "\n"


def member_name(series, weeks, release="COM", token="07d"):
    first, last = (week.replace("-", "") + "0000" for week in (weeks[0], weeks[-1]))
    return (f"H_ERA5_ECMW_T639_{series}_0000m_Pecd_SZON_S{first}_E{last}_NRG_TIM_{token}_{release}"
            f"_noc_org_NA_NA---_NA---_StRnF_PECD4.2_fv1.csv")


def member(series, weeks, values, release="COM", **text):
    return {member_name(series, weeks, release): member_text(weeks, values, **text)}


def write_archive(path, members):
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in members.items():
            archive.writestr(name, text)
    return path


def standard_members():
    """Annual files for the first three weeks and interim files for the fourth, for HRI and HOL."""
    return {**member("HRI", WEEKS[:3], [1.0, 2.0, 3.0]), **member("HRI", WEEKS[3:4], [4.0], "ITE"),
            **member("HOL", WEEKS[:3], [10.0, 20.0, 30.0]), **member("HOL", WEEKS[3:4], [40.0], "ITE"),
            **member("HRO", WEEKS[:3], [7.0, 7.0, 7.0])}


@pytest.fixture
def raw(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    monkeypatch.setattr(http_get, "RAW_DIR", raw_dir)
    monkeypatch.setattr(http_get, "MANIFEST", tmp_path / "raw_manifest.json")
    monkeypatch.setattr(config, "RAW_DIR", raw_dir)
    monkeypatch.setattr(pecd, "RAW", raw_dir / "pecd")
    return tmp_path


def build(raw, members):
    pecd.register(write_archive(raw / "download.zip", members))
    return pecd.build_weekly_inflow(pecd.registered_archives(), START_UTC, END_UTC)


def energy(table, series, week):
    row = table[(table["series"] == series) & (table["week_start_utc"] == pd.Timestamp(week, tz="UTC"))]
    assert len(row) == 1
    return row.iloc[0]


# --- weekly table -------------------------------------------------------------------------------------

def test_weekly_table_covers_every_week_overlapping_the_period(raw):
    table, _ = build(raw, standard_members())
    assert sorted(table["series"].unique()) == ["HOL", "HRI"]  # run of river (HRO) is not used
    assert sorted({w.date().isoformat() for w in table["week_start_utc"]}) == WEEKS[1:4]
    assert energy(table, "HRI", WEEKS[1])["energy_MWh"] == 2.0
    assert energy(table, "HOL", WEEKS[3])["energy_MWh"] == 40.0
    assert energy(table, "HOL", WEEKS[3])["release"] == "ITE"
    assert (table["week_end_utc"] - table["week_start_utc"] == pd.Timedelta(days=7)).all()


def test_missing_week_stays_missing(raw):
    members = standard_members()
    del members[member_name("HRI", WEEKS[:3])]
    members.update(member("HRI", WEEKS[:2], [1.0, 2.0]))
    table, record = build(raw, members)
    assert math.isnan(energy(table, "HRI", WEEKS[2])["energy_MWh"])
    assert record["weeks_without_value"] == {"HRI": [WEEKS[2]]}


def test_identical_overlap_is_merged_preferring_the_annual_file(raw):
    members = standard_members()
    members.update(member("HRI", WEEKS[2:4], [3.0, 4.0], "ITE"))  # repeats the annual value of 10-06
    del members[member_name("HRI", WEEKS[3:4], "ITE")]
    table, _ = build(raw, members)
    assert energy(table, "HRI", WEEKS[2])["release"] == "COM"


def test_conflicting_overlap_raises(raw):
    members = standard_members()
    members.update(member("HRI", WEEKS[2:4], [3.5, 4.0], "ITE"))
    del members[member_name("HRI", WEEKS[3:4], "ITE")]
    with pytest.raises(ValueError, match="conflicting"):
        build(raw, members)


def test_week_across_the_dst_change_keeps_its_energy(raw):
    members = {**member("HRI", WEEKS[4:], [168.0]), **member("HOL", WEEKS[4:], [16.8])}
    pecd.register(write_archive(raw / "download.zip", members))
    start, end = period_bounds_utc("2025-10-20", "2025-10-26")
    table, _ = pecd.build_weekly_inflow(pecd.registered_archives(), start, end)
    week = energy(table, "HRI", WEEKS[4])
    assert week["energy_MWh"] == 168.0
    assert week["week_end_utc"] - week["week_start_utc"] == pd.Timedelta(hours=168)


# --- validation ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    ({"unit": "GWh"}, "unit"),
    ({"resolution": "1 days"}, "resolution"),
    ({"zone": "FR00"}, "CH00"),
])
def test_file_format_deviation_raises(raw, change, message):
    members = {**standard_members(), **member("HRI", WEEKS[:3], [1.0, 2.0, 3.0], **change)}
    with pytest.raises(ValueError, match=message):
        build(raw, members)


def test_resolution_token_other_than_7_days_raises(raw):
    members = {member_name("HRI", WEEKS[:3], token="01d"): member_text(WEEKS[:3], [1.0, 2.0, 3.0])}
    with pytest.raises(ValueError, match="resolution"):
        build(raw, members)


def test_dates_not_7_days_apart_raise(raw):
    weeks = [WEEKS[0], WEEKS[2]]
    with pytest.raises(ValueError, match="7 days"):
        build(raw, {member_name("HRI", weeks): member_text(weeks, [1.0, 3.0])})


def test_dates_differing_from_the_name_raise(raw):
    with pytest.raises(ValueError, match="the name says"):
        build(raw, {member_name("HRI", WEEKS[:3]): member_text(WEEKS[1:4], [1.0, 2.0, 3.0])})


def test_file_without_weeks_raises(raw):
    with pytest.raises(ValueError, match="no weeks"):
        build(raw, {member_name("HRI", WEEKS[:1]): member_text([], [])})


def test_date_that_is_not_a_monday_raises(raw):
    weeks = ["2025-09-30"]
    with pytest.raises(ValueError, match="Monday"):
        build(raw, {member_name("HRI", weeks): member_text(weeks, [1.0])})


@pytest.mark.parametrize("value", [-1.0, "nan", "inf"])
def test_negative_or_non_finite_value_raises(raw, value):
    members = {**standard_members(), **member("HOL", WEEKS[:3], [10.0, value, 30.0])}
    with pytest.raises(ValueError, match="non-finite or negative"):
        build(raw, members)


@pytest.mark.parametrize("name", ["../" + member_name("HRI", WEEKS[:1]), "folder/" + member_name("HRI", WEEKS[:1])])
def test_unsafe_path_inside_the_archive_raises(raw, name):
    with pytest.raises(ValueError, match="unsafe path"):
        pecd.register(write_archive(raw / "download.zip", {name: member_text(WEEKS[:1], [1.0])}))


def test_unexpected_file_inside_the_archive_raises(raw):
    with pytest.raises(ValueError, match="unexpected file"):
        pecd.register(write_archive(raw / "download.zip", {**standard_members(), "readme.txt": "hello"}))


# --- registration -------------------------------------------------------------------------------------

def test_register_copies_the_archive_and_records_its_checksum(raw):
    key = pecd.register(write_archive(raw / "download.zip", standard_members()))
    copied = raw / "raw" / "pecd" / "download.zip"
    assert key == "pecd/download.zip" and copied.exists()
    assert http_get.load_manifest()[key]["sha256"] == http_get.sha256(copied)
    assert pecd.registered_archives() == [copied]


def test_archive_changed_since_registration_raises(raw):
    pecd.register(write_archive(raw / "download.zip", standard_members()))
    write_archive(raw / "raw" / "pecd" / "download.zip", {**standard_members(), **member("HRI", WEEKS[:1], [9.0])})
    with pytest.raises(RuntimeError, match="changed since it was registered"):
        pecd.registered_archives()


def test_unregistered_archive_raises(raw):
    (raw / "raw" / "pecd").mkdir()
    write_archive(raw / "raw" / "pecd" / "other.zip", standard_members())
    with pytest.raises(ValueError, match="not registered"):
        pecd.registered_archives()


def test_registered_archive_missing_on_disk_raises(raw):
    pecd.register(write_archive(raw / "download.zip", standard_members()))
    (raw / "raw" / "pecd" / "download.zip").unlink()
    with pytest.raises(FileNotFoundError, match="download.zip"):
        pecd.registered_archives()


# --- processing and provenance ------------------------------------------------------------------------

def test_processing_without_an_archive_removes_a_table_from_an_earlier_run(raw):
    out = raw / "processed"
    out.mkdir()
    (out / pecd.TABLE).write_text("stale")
    assert process.write_pecd_inflow(out, START_UTC, END_UTC) is None
    assert not (out / pecd.TABLE).exists()


def test_processing_again_after_replacing_the_archive_keeps_the_record_current(raw):
    out = raw / "processed"
    out.mkdir()
    pecd.register(write_archive(raw / "download.zip", standard_members()))
    first = process.write_pecd_inflow(out, START_UTC, END_UTC)
    replaced = {**standard_members(), **member("HRI", WEEKS[:3], [1.0, 2.5, 3.0])}
    pecd.register(write_archive(raw / "download.zip", replaced))
    second = process.write_pecd_inflow(out, START_UTC, END_UTC)
    assert first["archives"][0]["sha256"] != second["archives"][0]["sha256"]
    assert second["archives"][0]["sha256"] == http_get.sha256(raw / "raw" / "pecd" / "download.zip")
    table = pd.read_csv(out / pecd.TABLE, parse_dates=["week_start_utc"])
    assert energy(table, "HRI", WEEKS[1])["energy_MWh"] == 2.5
    assert {m["release"] for m in second["members"]} == {"COM", "ITE"}
    assert "0.825" in second["correction_factors"] and "UTC" in second["week_convention"]


def test_provenance_carries_the_pecd_record(raw, monkeypatch):
    monkeypatch.setattr(provenance, "git", lambda *args: "")
    monkeypatch.setattr(provenance, "raw_files", lambda manifest: {})
    out = raw / "processed"
    out.mkdir()
    provenance.write(out, {"hours": 1}, extra={"pecd": {"zone": "CH00"}})
    assert json.loads((out / "provenance.json").read_text(encoding="utf-8"))["pecd"] == {"zone": "CH00"}
