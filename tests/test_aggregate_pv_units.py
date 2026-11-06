"""data_prep/aggregate_pv_units.py: per-bus sums, clipping, the yield threshold and the written format."""
import numpy as np
import pandas as pd
import pytest

from data_prep import aggregate_pv_units as agg
from model.high_res_PV import HOUR_COLS, PROFILES_FILE, UNITS_FILE


def dataset(tmp_path, units, output, year=2050):
    """A source dataset folder with the given units and hourly output rows (MWh per hour)."""
    folder = tmp_path / "src"
    folder.mkdir()
    pd.DataFrame(units).to_csv(folder / UNITS_FILE, index=False)
    profiles = pd.DataFrame(np.array(output, dtype=float), columns=HOUR_COLS)
    profiles.insert(0, "year", year)
    profiles.insert(0, "Name", [u["GenName"] for u in units])
    profiles.to_csv(folder / PROFILES_FILE, index=False)
    return str(folder)


def unit(name, bus, pmax, technology="PV-roof"):
    return {"GenName": name, "Pmax": pmax, "Technology": technology, "BusName": bus}


def flat(value):
    return [value] * 8760


def test_read_dataset_clips_output_to_pmax_and_skips_other_technologies(tmp_path):
    folder = dataset(tmp_path, [unit("a", "B1", 1.0), unit("b", "B1", 2.0, "PV-alpine")], [flat(1.2), flat(1.0)])
    units, output = agg.read_dataset(folder, 2050)
    assert units["GenName"].tolist() == ["a"]
    assert output.max() == 1.0


def test_read_dataset_refuses_a_second_profile_year(tmp_path):
    folder = dataset(tmp_path, [unit("a", "B1", 1.0)], [flat(0.5)], year=2040)
    with pytest.raises(ValueError, match="profile years"):
        agg.read_dataset(folder, 2050)


def test_aggregate_by_bus_sums_pmax_and_output_per_bus():
    units = pd.DataFrame([unit("a", "B1", 1.0), unit("b", "B1", 3.0), unit("c", "B2", 2.0)])
    output = np.array([flat(0.5), flat(0.5), flat(1.0)])
    agg_units, agg_output = agg.aggregate_by_bus(units, output, np.array([True, True, True]))
    assert agg_units["Pmax"].tolist() == [4.0, 2.0]
    assert agg_units["n_units"].tolist() == [2, 1]
    # availability of the bus unit = summed output / summed Pmax = 1.0 / 4.0
    assert agg_output[0, 0] / agg_units["Pmax"].iloc[0] == pytest.approx(0.25)


def test_keep_best_until_keeps_the_highest_yields_until_the_potential_is_reached():
    flh = np.array([500.0, 1200.0, 900.0, 1100.0])
    pmax = np.array([10.0, 5.0, 10.0, 5.0])
    keep, threshold = agg.keep_best_until(flh, pmax, 12.0)
    assert keep.tolist() == [False, True, True, True]   # 1200 + 1100 give 10 MW < 12, so the 900 unit is kept too
    assert threshold == 900.0


def test_keep_best_until_refuses_more_potential_than_exists():
    with pytest.raises(ValueError):
        agg.keep_best_until(np.array([1.0]), np.array([1.0]), 2.0)


def test_write_dataset_refuses_an_existing_folder(tmp_path):
    units = pd.DataFrame([{"GenName": "x", "Pmax": 1.0, "Technology": "PV-roof", "BusName": "B1"}])
    agg.write_dataset(str(tmp_path / "out"), units, np.array([flat(0.1)]), 2050, ["test"])
    with pytest.raises(FileExistsError):
        agg.write_dataset(str(tmp_path / "out"), units, np.array([flat(0.1)]), 2050, ["test"])
