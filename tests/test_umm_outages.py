"""Unit tests for data_prep/umm_outages.py: plant outages for a back-cast from EEX urgent market messages."""
import json

import numpy as np
import pandas as pd
import pytest

from data_prep import umm_outages as um

HEADER = ["unavailabilityType", "eventType", "companyName", "companyID", "facilityName", "unitName", "eIC", "aCE",
          "shareholders", "biddingZone", "fuelType", "eventStart", "eventStop", "installedCapacity",
          "availableCapacity", "nonAvailableCapacity", "unavailabilityReason", "remarks", "eventStatus", "messageID",
          "modified", "unitMeasure", "capacityIntervals", "direction"]


def message(message_id, eic="U1", start="2025-10-01T10:00:00.000Z", stop="2025-10-01T12:00:00.000Z", unavailable=50.0,
            installed=100.0, status="Active", event="Production unavailability", fuel="Hydro Water Reservoir",
            profile=None):
    row = dict.fromkeys(HEADER)
    row.update({"eventType": event, "facilityName": "F", "unitName": f"unit {eic}", "eIC": eic, "fuelType": fuel,
                "eventStart": start, "eventStop": stop, "installedCapacity": installed,
                "nonAvailableCapacity": unavailable, "availableCapacity": installed - unavailable,
                "eventStatus": status, "messageID": message_id, "capacityIntervals": profile or []})
    return [row[h] for h in HEADER]


def eex_file(tmp_path, name, rows):
    path = tmp_path / name
    path.write_text(json.dumps({"header": HEADER, "capacityIntervalsHeader": ["validFrom", "validTo",
                                "nonAvailableCapacity", "availableCapacity"], "data": rows}), encoding="utf-8")
    return str(path)


HOURS = pd.date_range("2025-10-01 09:00", "2025-10-01 14:00", freq="h", inclusive="left", tz="UTC")


# --- messages -----------------------------------------------------------------------------------------------------

def test_the_newest_version_of_each_message_is_kept_across_files(tmp_path):
    first = eex_file(tmp_path, "a.json", [message("#1_001", unavailable=40.0), message("#2_001", eic="U2")])
    second = eex_file(tmp_path, "b.json", [message("#1_002", unavailable=60.0), message("#2_001", eic="U2")])
    messages = um.read_messages([first, second])
    assert sorted(messages["eic"]) == ["U1", "U2"]  # #2_001 is in both files, read once
    assert messages.loc[messages["eic"] == "U1", "unavailable_MW"].tolist() == [60.0]


@pytest.mark.parametrize("newest", [
    dict(status="Dismissed"), dict(event="Other unavailability"), dict(fuel="Nuclear"),
    dict(fuel="Hydro Run-of-river and poundage"),
])
def test_a_dismissed_or_other_newest_version_drops_the_event(tmp_path, newest):
    path = eex_file(tmp_path, "a.json", [message("#1_001"), message("#1_002", **newest)])
    assert um.read_messages([path]).empty


def test_pumped_storage_messages_are_kept(tmp_path):
    path = eex_file(tmp_path, "a.json", [message("#1_001", fuel="Hydro Pumped Storage")])
    assert len(um.read_messages([path])) == 1


def test_a_capacity_profile_raises(tmp_path):
    path = eex_file(tmp_path, "a.json", [message("#1_001", profile=[["2025-10-01T10:00:00.000Z", "x", 10, 90]])])
    with pytest.raises(ValueError, match="capacity profile"):
        um.read_messages([path])


# --- unit hours -----------------------------------------------------------------------------------------------------

def messages_frame(rows):
    return pd.DataFrame(rows, columns=["eic", "unit", "start", "stop", "unavailable_MW", "installed_MW"]).assign(
        start=lambda f: pd.to_datetime(f["start"], utc=True), stop=lambda f: pd.to_datetime(f["stop"], utc=True))


def test_partial_hours_are_weighted_by_their_overlap():
    frame = messages_frame([("U1", "u", "2025-10-01 10:30", "2025-10-01 12:15", 100.0, 200.0)])
    unit = um.unit_unavailable(frame, HOURS)
    assert unit["U1"].tolist() == pytest.approx([0.0, 50.0, 100.0, 25.0, 0.0])


def test_concurrent_messages_add_up_to_at_most_the_installed_capacity():
    frame = messages_frame([("U1", "u", "2025-10-01 09:00", "2025-10-01 11:00", 60.0, 100.0),
                            ("U1", "u", "2025-10-01 10:00", "2025-10-01 12:00", 60.0, 100.0)])
    assert um.unit_unavailable(frame, HOURS)["U1"].tolist() == pytest.approx([60.0, 100.0, 60.0, 0.0, 0.0])


# --- plants ---------------------------------------------------------------------------------------------------------

UNIT_MAP = pd.DataFrame({"eic": ["U1", "U2", "U3"], "plants": ["dam_a", "dam_b;psp_c", ""],
                         "note": ["", "", "not in the fleet: run-of-river"]})
GEN_MAX = {"dam_a": 90.0, "dam_b": 120.0, "psp_c": 60.0}


def test_a_unit_passes_its_MW_to_one_plant_or_shares_them_by_capacity():
    unit = pd.DataFrame({"U1": [30.0, 0.0], "U2": [100.0, 200.0], "U3": [50.0, 50.0]}, index=HOURS[:2])
    plants, left_out = um.plant_unavailable(unit, UNIT_MAP, GEN_MAX, {"U1": 100.0, "U2": 200.0, "U3": 50.0})
    assert plants["dam_a"].tolist() == [30.0, 0.0]
    assert plants["dam_b"].tolist() == pytest.approx([60.0, 120.0])  # 120 / 200 of the unit
    assert plants["psp_c"].tolist() == pytest.approx([30.0, 60.0])
    assert left_out == {"U3": "not in the fleet: run-of-river"}


def test_a_unit_missing_from_the_map_or_a_plant_outside_the_fleet_raises():
    unit = pd.DataFrame({"U9": [10.0]}, index=HOURS[:1])
    with pytest.raises(ValueError, match=r"units \['U9'\] are not in the unit map"):
        um.plant_unavailable(unit, UNIT_MAP, GEN_MAX, {"U9": 10.0})
    bad_map = pd.DataFrame({"eic": ["U1"], "plants": ["dam_x"], "note": [""]})
    with pytest.raises(ValueError, match=r"dam_x.* of unit U1 are not in the fleet"):
        um.plant_unavailable(pd.DataFrame({"U1": [10.0]}, index=HOURS[:1]), bad_map, GEN_MAX, {"U1": 100.0})


# --- windows --------------------------------------------------------------------------------------------------------

def test_windows_merge_equal_hours_clip_at_zero_and_scale_the_pumps():
    plants = pd.DataFrame({"psp_c": [0.0, 15.0, 15.0, 90.0, 0.0]}, index=HOURS)
    windows = um.availability_windows(plants, {"psp_c": 60.0}, {"psp_c": 40.0}, {"psp_c": "EEX UMM U2"})
    assert windows.to_dict("list") == {
        "plant": ["psp_c", "psp_c"], "start_utc": ["2025-10-01 10:00", "2025-10-01 12:00"],
        "end_utc": ["2025-10-01 12:00", "2025-10-01 13:00"], "turbine_MW": [45.0, 0.0], "pump_MW": [30.0, 0.0],
        "source": ["EEX UMM U2", "EEX UMM U2"]}


def test_capacities_are_rounded_to_a_tenth_of_a_MW():
    plants = pd.DataFrame({"dam_a": [10.04, 10.01, 0.0, 0.0, 0.0]}, index=HOURS)
    windows = um.availability_windows(plants, {"dam_a": 90.0}, {"dam_a": 0.0}, {"dam_a": "s"})
    assert windows[["start_utc", "end_utc", "turbine_MW"]].values.tolist() == [["2025-10-01 09:00", "2025-10-01 11:00", 80.0]]
    assert np.isclose(windows["pump_MW"], 0.0).all()
