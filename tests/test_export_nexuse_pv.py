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


def _record(name, pmax, peak, bus="CH_Bus_220", year=2050, technology="PV-roof",
            inv_cost=64921.0, fom=24950.0):
    """A candidate record whose profile is a flat `peak` for 8760 hours."""
    return {
        "GenName": name, "Pmax": pmax, "Technology": technology, "BusName": bus,
        "idProfile": hash(name) % 10000, "InvCost": inv_cost, "FOM_Cost": fom,
        "year": year, "timeSeries": _blob([peak] * HOURS),
    }


def _unit(name, pmax=1.0, bus="A", inv_cost=1.0, fom=2.0):
    return {"GenName": name, "Pmax": pmax, "Technology": "PV-roof", "BusName": bus,
            "idProfile": 1, "InvCost": inv_cost, "FOM_Cost": fom}


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


# --- assign_cost_class -----------------------------------------------------------

def test_assign_cost_class_from_the_name_suffix_or_all():
    assert export.assign_cost_class("CH_RES_PV_CH_Aathal_220_15_0_1-10kW", "name-suffix") == "1-10kW"
    assert export.assign_cost_class("CH_RES_PV_CH_Aathal_220_0_180_100-1000kW", "name-suffix") == "100-1000kW"
    assert export.assign_cost_class("CH_RES_PV_Cd1_Pradella_380", "all") == "all"
    assert export.assign_cost_class("CH_RES_PV_CH_Aathal_220_15_0_1-10kW", "all") == "all"


def test_assign_cost_class_rejects_names_without_a_class_and_unknown_modes():
    with pytest.raises(ValueError, match="'380' is not a size class"):
        export.assign_cost_class("CH_RES_PV_Cd1_Pradella_380", "name-suffix")
    with pytest.raises(ValueError, match="'1-20kW' is not a size class"):
        export.assign_cost_class("CH_RES_PV_x_1-20kW", "name-suffix")
    with pytest.raises(ValueError, match="mode must be one of"):
        export.assign_cost_class("anything", "guess")


# --- build_units_table -----------------------------------------------------------

def test_build_units_table_keeps_columns_and_order():
    rows = [dict(_unit("b", 2.0, "B"), extra=1), dict(_unit("a", 1.0, "A"), extra=2)]
    units = export.build_units_table(rows, "all")
    assert list(units.columns) == export.UNIT_COLUMNS
    assert units["GenName"].tolist() == ["b", "a"]


def test_build_units_table_adds_the_class_and_renames_the_nexuse_costs():
    rows = [_unit("CH_RES_PV_CH_A_220_15_0_1-10kW", inv_cost=105903.7, fom=26144.1),
            _unit("CH_RES_PV_CH_A_220_15_0_30-100kW", inv_cost=55671.0, fom=13743.3)]
    units = export.build_units_table(rows, "name-suffix")
    assert units["cost_class"].tolist() == ["1-10kW", "30-100kW"]
    assert units["nexuse_InvCost_EURperMWyr"].tolist() == [105903.7, 55671.0]
    assert units["nexuse_FOM_EURperMWyr"].tolist() == [26144.1, 13743.3]
    assert "InvCost" not in units.columns and "FOM_Cost" not in units.columns


def test_build_units_table_rejects_duplicates_zero_pmax_empty_and_missing_cost_columns():
    good = _unit("a")
    with pytest.raises(ValueError, match="duplicate"):
        export.build_units_table([good, dict(good)], "all")
    with pytest.raises(ValueError, match="Pmax <= 0"):
        export.build_units_table([dict(good, Pmax=0.0)], "all")
    with pytest.raises(ValueError, match="no candidate"):
        export.build_units_table([], "all")
    without_costs = {k: v for k, v in good.items() if k not in ("InvCost", "FOM_Cost")}
    with pytest.raises(ValueError, match=r"lack column\(s\) \['InvCost', 'FOM_Cost'\]"):
        export.build_units_table([without_costs], "all")


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

    summary = export.export_records(records, str(out), ["synthetic"], "all")

    assert summary == {
        "units": 2, "buses": 2, "pmax_sum_mw": 14.0,
        "energy_twh_at_pmax": pytest.approx((2.0 + 7.5) * HOURS / 1e6),
        "profile_years": {2050: 2},
        "units_above_pmax": 0, "max_overshoot_mw": 0.0,
    }
    units = pd.read_csv(out / "units.csv")
    assert list(units.columns) == export.UNIT_COLUMNS
    assert units["cost_class"].tolist() == ["all", "all"]
    assert units["nexuse_InvCost_EURperMWyr"].tolist() == [64921.0, 64921.0]
    profiles = pd.read_csv(out / "NexusInput_profiles_newPV_mv.csv")
    assert list(profiles.columns[:7]) == export.PROFILE_META_COLUMNS
    assert profiles.shape == (2, 7 + HOURS)
    assert profiles.loc[0, "Name"] == "CH_RES_PV_a-1" and profiles.loc[0, "year"] == 2050
    source = (out / "SOURCE.md").read_text(encoding="utf-8")
    assert "synthetic" in source and "sum Pmax = 14.0 MW" in source
    assert "Cost classes assigned with --cost-class all: {'all': 2}" in source

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
        export.export_records(records, str(tmp_path / "ds" / "2009"), [], "all")
    assert not (tmp_path / "ds" / "2009" / "units.csv").exists()


def test_export_records_counts_units_slightly_above_pmax(tmp_path):
    """A rounding excess within tolerance is kept, counted and written to SOURCE.md."""
    records = [_record("tiny", 0.01607, 0.0162), _record("ok", 4.0, 2.0)]
    out = tmp_path / "ds" / "2009"

    summary = export.export_records(records, str(out), [], "all")

    assert summary["units"] == 2
    assert summary["units_above_pmax"] == 1
    assert summary["max_overshoot_mw"] == pytest.approx(0.00013)
    assert "profile peaks above Pmax" in (out / "SOURCE.md").read_text(encoding="utf-8")
    assert "0.13 kW" in (out / "SOURCE.md").read_text(encoding="utf-8")


def test_export_records_stops_on_a_name_without_a_class_in_name_suffix_mode(tmp_path):
    records = [_record("CH_RES_PV_CH_A_220_15_0_1-10kW", 4.0, 2.0), _record("CH_RES_PV_Cd1_Pradella_380", 4.0, 2.0)]
    with pytest.raises(ValueError, match="Pradella_380: last name token '380' is not a size class"):
        export.export_records(records, str(tmp_path / "ds" / "2009"), [], "name-suffix")


# --- export_units_only -----------------------------------------------------------

def test_export_units_only_rewrites_the_unit_table_and_keeps_the_profiles(tmp_path):
    out = tmp_path / "ds" / "2009"
    names = ["CH_RES_PV_CH_A_220_15_0_1-10kW", "CH_RES_PV_CH_B_220_0_180_30-100kW"]
    export.export_records([_record(names[0], 4.0, 2.0), _record(names[1], 10.0, 7.5)], str(out), ["first"], "all")
    profiles_before = (out / "NexusInput_profiles_newPV_mv.csv").read_bytes()

    summary = export.export_units_only(
        [_unit(names[0], 4.0, "A", inv_cost=105903.7), _unit(names[1], 10.0, "B", inv_cost=55671.0)],
        str(out), ["second"], "name-suffix",
    )

    assert summary == {"units": 2, "buses": 2, "pmax_sum_mw": 14.0, "cost_classes": {"1-10kW": 1, "30-100kW": 1}}
    units = pd.read_csv(out / "units.csv")
    assert units["cost_class"].tolist() == ["1-10kW", "30-100kW"]
    assert units["nexuse_InvCost_EURperMWyr"].tolist() == [105903.7, 55671.0]
    assert (out / "NexusInput_profiles_newPV_mv.csv").read_bytes() == profiles_before
    source = (out / "SOURCE.md").read_text(encoding="utf-8")
    assert "first" in source and "second" in source
    assert "--cost-class name-suffix: {'1-10kW': 1, '30-100kW': 1}" in source


def test_export_units_only_rejects_units_that_do_not_match_the_profiles(tmp_path):
    out = tmp_path / "ds" / "2009"
    export.export_records([_record("CH_RES_PV_a", 4.0, 2.0)], str(out), [], "all")
    units_before = (out / "units.csv").read_bytes()

    with pytest.raises(ValueError, match=r"1 unit\(s\) without profile \(e.g. \['CH_RES_PV_b'\]\), 1 profile\(s\) without unit"):
        export.export_units_only([_unit("CH_RES_PV_b", 4.0)], str(out), [], "all")
    assert (out / "units.csv").read_bytes() == units_before


def test_export_units_only_needs_an_exported_folder(tmp_path):
    with pytest.raises(FileNotFoundError, match="export the dataset in full first"):
        export.export_units_only([_unit("a")], str(tmp_path / "ds" / "2009"), [], "all")
