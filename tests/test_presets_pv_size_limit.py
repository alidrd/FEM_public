"""Unit tests for data_prep/presets_pv_size_limit.py, which keeps PV-roof cost classes out of investment."""
from pathlib import Path

import pytest

from data_prep.presets_pv_size_limit import excluded_plants, write_presets
from model.variable_presets import read_variable_presets

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "input/variable_presets/neighbours_wy2009_fr_oil_no_new_ch_nuclear_no_fuel_allowance.csv"
UP_TO_30_KW = REPO / "input/variable_presets/neighbours_wy2009_fr_oil_no_new_ch_nuclear_no_fuel_allowance_pv_up_to_30kw.csv"
SMALL = {"1-10kW", "10-30kW"}
CLASSES = {"CH00_a_pvrf": "1-10kW", "CH00_b_pvrf": "30-100kW", "CH00_c_pvrf": "10-30kW", "CH00_d_pvrf": "100-1000kW"}


def test_only_plants_outside_the_allowed_classes_are_excluded():
    assert excluded_plants(CLASSES, SMALL) == ["CH00_b_pvrf", "CH00_d_pvrf"]


def test_an_allowed_class_missing_from_the_dataset_raises():
    with pytest.raises(ValueError, match="10-30 kW"):
        excluded_plants(CLASSES, {"1-10kW", "10-30 kW"})


def test_written_file_keeps_the_base_rows_and_fixes_each_excluded_plant_at_zero(tmp_path):
    base = tmp_path / "base.csv"
    base.write_text("# header\ngen_max,,CH00_nuclear,,0.01\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    write_presets(base, ["CH00_b_pvrf", "CH00_d_pvrf"], out, "test")
    rows = [(p["variable_name"], p["indices"][0], p["value"]) for p in read_variable_presets(str(out))]
    assert rows == [("gen_max", "CH00_nuclear", 0.01), ("gen_max", "CH00_b_pvrf", 0.0), ("gen_max", "CH00_d_pvrf", 0.0)]


@pytest.mark.skipif(not (REPO / "input/RES_nexuse/PV/pv09_ewopt/2009/units.csv").exists(),
                    reason="dataset pv09_ewopt/2009 is not in git (input/RES_nexuse/ is ignored)")
def test_up_to_30kw_presets_fix_exactly_the_larger_candidates_on_top_of_the_base_file():
    from model.high_res_PV import load_cost_classes
    base = {(p["variable_name"], tuple(p["indices"]), p["value"]) for p in read_variable_presets(str(BASE))}
    rows = [(p["variable_name"], tuple(p["indices"]), p["value"]) for p in read_variable_presets(str(UP_TO_30_KW))]
    added = [r for r in rows if r not in base]
    assert base <= set(rows) and len(rows) == len(set(rows))
    assert added == [("gen_max", (plant, ""), 0.0) for plant in excluded_plants(load_cost_classes("pv09_ewopt", 2009), SMALL)]
