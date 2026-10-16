"""Unit tests for data_prep/export_nexuse_pv.py, the part that needs no database.

The database access is one generator (`fetch_candidates`); everything after it
takes plain records, so the tests feed synthetic records and check the files
the export writes and the errors it raises on bad data. The written folder is
then read back with the loader to prove the two agree on the layout.
"""
import json
from pathlib import Path

import pandas as pd
import pytest

import data_prep.export_nexuse_pv as export
import model.high_res_PV as hires

HOURS = export.HOURS_PER_YEAR


def _blob(values):
    return json.dumps(values)


def _record(name, pmax, peak, bus="CH_Bus_220", year=2050, technology="PV-roof"):
    """A candidate record whose profile is a flat `peak` for 8760 hours."""
    return {
        "GenName": name, "Pmax": pmax, "Technology": technology, "BusName": bus,
        "idProfile": hash(name) % 10000, "year": year, "timeSeries": _blob([peak] * HOURS),
    }


# --- parse_profile ---------------------------------------------------------------

def test_parse_profile_returns_8760_floats():
    values = export.parse_profile(_blob([0, 1.5] * (HOURS // 2)))
    assert len(values) == HOURS
    assert values[:2] == [0.0, 1.5]
    assert all(isinstance(v, float) for v in values)


def test_parse_profile_rejects_wrong_length():
    with pytest.raises(ValueError, match="8760"):
        export.parse_profile(_blob([0.0] * (HOURS - 1)))


def test_parse_profile_rejects_non_numeric_and_non_json():
    with pytest.raises(ValueError, match="non-numeric"):
        export.parse_profile(_blob([0.0] * (HOURS - 1) + ["x"]))
    with pytest.raises(ValueError, match="not JSON"):
        export.parse_profile("[0.0, 0.0")


# --- check_profile_against_pmax --------------------------------------------------

def test_check_profile_accepts_values_up_to_pmax_and_reports_no_excess():
    assert export.check_profile_against_pmax("u", [0.0, 5.0, 10.0], 10.0) == 0.0


def test_check_profile_tolerates_rounding_excess_and_reports_it():
    """Up to 1 kW above Pmax passes and the excess is returned for the summary."""
    excess = export.check_profile_against_pmax("u", [0.0, 0.0162], 0.01607)
    assert excess == pytest.approx(0.00013)
    assert export.check_profile_against_pmax("u", [0.0, 10.001], 10.0) == pytest.approx(0.001)


def test_check_profile_rejects_negative_and_well_above_pmax():
    with pytest.raises(ValueError, match="negative"):
        export.check_profile_against_pmax("u", [0.0, -0.1], 10.0)
    with pytest.raises(ValueError, match="exceeds Pmax"):
        export.check_profile_against_pmax("u", [0.0, 10.0011], 10.0)


# --- build_units_table -----------------------------------------------------------

def test_build_units_table_keeps_columns_and_order():
    rows = [
        {"GenName": "b", "Pmax": 2.0, "Technology": "PV-roof", "BusName": "B", "idProfile": 2, "extra": 1},
        {"GenName": "a", "Pmax": 1.0, "Technology": "PV-roof", "BusName": "A", "idProfile": 1, "extra": 2},
    ]
    units = export.build_units_table(rows)
    assert list(units.columns) == export.UNIT_COLUMNS
    assert units["GenName"].tolist() == ["b", "a"]


def test_build_units_table_rejects_duplicates_zero_pmax_and_empty():
    good = {"GenName": "a", "Pmax": 1.0, "Technology": "PV-roof", "BusName": "A", "idProfile": 1}
    with pytest.raises(ValueError, match="duplicate"):
        export.build_units_table([good, dict(good)])
    with pytest.raises(ValueError, match="Pmax <= 0"):
        export.build_units_table([dict(good, Pmax=0.0)])
    with pytest.raises(ValueError, match="no candidate"):
        export.build_units_table([])


# --- iterate_in_batches ----------------------------------------------------------

def test_iterate_in_batches_walks_ids_and_stops_on_short_batch():
    ids = [3, 5, 8, 13, 21]
    calls = []

    def fetch_batch(last_id, batch_size):
        calls.append(last_id)
        return [{"idGen": i} for i in ids if i > last_id][:batch_size]

    out = [r["idGen"] for r in export.iterate_in_batches(fetch_batch, batch_size=2)]
    assert out == ids
    assert calls == [0, 5, 13]            # the short batch [21] ends the loop without another fetch


def test_iterate_in_batches_handles_exact_multiple():
    """A total that is a multiple of the batch size needs one extra, empty fetch."""
    def fetch_batch(last_id, batch_size):
        return [{"idGen": i} for i in [1, 2, 3, 4] if i > last_id][:batch_size]

    assert [r["idGen"] for r in export.iterate_in_batches(fetch_batch, batch_size=2)] == [1, 2, 3, 4]


# --- export_records --------------------------------------------------------------

def test_export_records_writes_files_the_loader_reads(tmp_path, monkeypatch):
    records = [_record("CH_RES_PV_a-1", 4.0, 2.0), _record("CH_RES_PV_b", 10.0, 7.5, bus="CH_Other_380")]
    out = tmp_path / "ds" / "2009"

    summary = export.export_records(records, str(out), ["synthetic"])

    assert summary == {
        "units": 2, "buses": 2, "pmax_sum_mw": 14.0,
        "energy_twh_at_pmax": pytest.approx((2.0 + 7.5) * HOURS / 1e6),
        "profile_years": {2050: 2},
        "units_above_pmax": 0, "max_overshoot_mw": 0.0,
    }
    units = pd.read_csv(out / "units.csv")
    assert list(units.columns) == export.UNIT_COLUMNS
    profiles = pd.read_csv(out / "NexusInput_profiles_newPV_mv.csv")
    assert list(profiles.columns[:7]) == export.PROFILE_META_COLUMNS
    assert profiles.shape == (2, 7 + HOURS)
    assert profiles.loc[0, "Name"] == "CH_RES_PV_a-1" and profiles.loc[0, "year"] == 2050
    source = (out / "SOURCE.md").read_text(encoding="utf-8")
    assert "synthetic" in source and "sum Pmax = 14.0 MW" in source

    # the loader reads the folder back: availability = profile / Pmax
    monkeypatch.setattr(hires, "PV_DIR", str(tmp_path))
    hires.load_high_res_PV.cache_clear()
    plants, avail = hires.load_high_res_PV("ds", 2009, 2050)
    assert set(plants.index) == {"CH00_CH_RES_PV_a_1_pvrf", "CH00_CH_RES_PV_b_pvrf"}
    assert avail[("CH00_CH_RES_PV_a_1_pvrf", "t_1")] == pytest.approx(0.5)
    assert avail[("CH00_CH_RES_PV_b_pvrf", "t_8760")] == pytest.approx(0.75)
    hires.load_high_res_PV.cache_clear()


def test_export_records_stops_on_profile_above_pmax(tmp_path):
    records = [_record("ok", 4.0, 2.0), _record("bad", 4.0, 4.5)]
    with pytest.raises(ValueError, match="bad: profile maximum 4.5 exceeds Pmax 4.0"):
        export.export_records(records, str(tmp_path / "ds" / "2009"), [])
    assert not (tmp_path / "ds" / "2009" / "units.csv").exists()


def test_export_records_counts_units_slightly_above_pmax(tmp_path):
    """A rounding excess within tolerance is kept, counted and written to SOURCE.md."""
    records = [_record("tiny", 0.01607, 0.0162), _record("ok", 4.0, 2.0)]
    out = tmp_path / "ds" / "2009"

    summary = export.export_records(records, str(out), [])

    assert summary["units"] == 2
    assert summary["units_above_pmax"] == 1
    assert summary["max_overshoot_mw"] == pytest.approx(0.00013)
    assert "profile peaks above Pmax" in (out / "SOURCE.md").read_text(encoding="utf-8")
    assert "0.13 kW" in (out / "SOURCE.md").read_text(encoding="utf-8")

