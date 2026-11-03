"""Unit tests for the high-resolution PV loader (model/high_res_PV.py).

Covers the loader's contract (docs/high_res_pv.md):
  1. Happy path — shape, columns, plant IDs, availability dict size.
  2. Plant-ID sanitization — `-` becomes `_`, no `-` survives.
  3. Availability scaling and clipping — Pmax/2 -> 0.5; >Pmax -> 1.0; <0 -> 0.0.
  4. Pmax <= 0 filter, orphan units and orphan profiles (dropped and logged).
  5. Run-year exact match and closest-year fallback; missing weather-year folder raises.
  6. Caching of repeated calls.
  7. Schema compatibility — returned DataFrame columns match
     input/plants_invest_candidates_res_CH.csv so the rows concatenate cleanly.
  8. Cost classes — `load_cost_classes` reads the optional `cost_class` column.
  9. `replace_aggregated_pv_candidates` drops exactly the seven regional rows.

These tests construct small synthetic input files in a `tmp_path`
fixture (layout `<dataset>/<weather_year>/`) and monkeypatch ``model.high_res_PV.PV_DIR`` to point at the
fixture root. The lru_cache on ``load_high_res_PV`` is cleared
between tests so independent fixtures do not collide.
"""
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import model.high_res_PV as hires


REPO_ROOT = Path(__file__).resolve().parent.parent
CANDIDATES_CSV = REPO_ROOT / "input" / "plants_invest_candidates_res_CH.csv"


DATASET = "ds"


def _write_fixture(tmp_path: Path, units_rows: list[dict], profile_rows: list[dict],
                   weather_year: int = 2009, dataset: str = DATASET) -> Path:
    """Write a units.csv + NexusInput_profiles_newPV_mv.csv pair under
    tmp_path/<dataset>/<weather_year>/ and return the parent (the PV root)."""
    pv_root = tmp_path
    folder = pv_root / dataset / str(weather_year)
    folder.mkdir(parents=True, exist_ok=True)

    units_df = pd.DataFrame(units_rows)
    units_df.to_csv(folder / "units.csv", index=False)

    profiles_df = pd.DataFrame(profile_rows)
    profiles_df.to_csv(folder / "NexusInput_profiles_newPV_mv.csv", index=False)
    return pv_root


def _profile_row(name: str, year: int, value: float) -> dict:
    """Return a profile row with constant `value` for all 8760 hours."""
    row = {"Name": name, "year": year}
    for h in range(1, 8761):
        row[str(h)] = value
    return row


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    """Clear the loader caches before and after each test for isolation."""
    hires.load_high_res_PV.cache_clear()
    hires.load_cost_classes.cache_clear()
    yield
    hires.load_high_res_PV.cache_clear()
    hires.load_cost_classes.cache_clear()


@pytest.fixture
def patched_pv_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(hires, "PV_DIR", str(tmp_path))
    return tmp_path


def test_happy_path(patched_pv_dir):
    """Verify the expected output structure and baseline availability values."""
    units = [
        {"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"},
        {"GenName": "beta",  "Pmax": 2.0, "Technology": "PV-roof"},
        # a non-PV-roof row that must be filtered out
        {"GenName": "windy", "Pmax": 5.0, "Technology": "Wind"},
    ]
    profiles = [
        _profile_row("alpha", 2050, 2.0),  # af = 2/4 = 0.5
        _profile_row("beta",  2050, 1.0),  # af = 1/2 = 0.5
    ]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, avail = hires.load_high_res_PV(DATASET, 2009, 2050)

    # plant_records shape and index
    assert len(plant_records) == 2
    assert set(plant_records.index) == {"CH00_alpha_pvrf", "CH00_beta_pvrf"}

    # required column values
    assert (plant_records["node"] == "CH00").all()
    assert (plant_records["market"] == "CH00").all()
    assert (plant_records["n_redispatch"] == "CH00").all()
    assert (plant_records["plant_type"] == "RES").all()
    assert (plant_records["tech"] == "pvrf").all()
    assert plant_records.loc["CH00_alpha_pvrf", "gen_max_limit"] == 4.0
    assert plant_records.loc["CH00_beta_pvrf",  "gen_max_limit"] == 2.0
    assert np.isinf(plant_records["energy_max_limit"]).all()

    # avail dict has 8760 entries per plant
    assert len(avail) == 8760 * 2
    # all in [0, 1]
    assert all(0.0 <= v <= 1.0 for v in avail.values())
    # spot check a sample
    assert avail[("CH00_alpha_pvrf", "t_1")] == pytest.approx(0.5)
    assert avail[("CH00_beta_pvrf",  "t_8760")] == pytest.approx(0.5)


def test_plant_id_sanitization(patched_pv_dir):
    """Ensure hyphens in source names are sanitized to underscores in IDs."""
    units = [
        {"GenName": "foo-bar-baz", "Pmax": 1.0, "Technology": "PV-roof"},
    ]
    profiles = [_profile_row("foo-bar-baz", 2050, 0.3)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, avail = hires.load_high_res_PV(DATASET, 2009, 2050)

    pid = plant_records.index[0]
    assert pid == "CH00_foo_bar_baz_pvrf"
    assert "-" not in pid
    # availability dict keys must use the sanitized id too
    for (plant, _t) in avail:
        assert "-" not in plant


def test_availability_scaling_and_clipping(patched_pv_dir):
    """Check availability normalization by Pmax and clipping to [0, 1]."""
    # plant 'half'   has Pmax=10, profile=5  -> af = 0.5
    # plant 'over'   has Pmax=10, profile=20 -> af clipped to 1.0
    # plant 'neg'    has Pmax=10, profile=-3 -> af clipped to 0.0
    units = [
        {"GenName": "half", "Pmax": 10.0, "Technology": "PV-roof"},
        {"GenName": "over", "Pmax": 10.0, "Technology": "PV-roof"},
        {"GenName": "neg",  "Pmax": 10.0, "Technology": "PV-roof"},
    ]
    profiles = [
        _profile_row("half", 2050,  5.0),
        _profile_row("over", 2050, 20.0),
        _profile_row("neg",  2050, -3.0),
    ]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    _, avail = hires.load_high_res_PV(DATASET, 2009, 2050)

    assert avail[("CH00_half_pvrf", "t_100")] == pytest.approx(0.5)
    assert avail[("CH00_over_pvrf", "t_100")] == pytest.approx(1.0)
    assert avail[("CH00_neg_pvrf",  "t_100")] == pytest.approx(0.0)


def test_pmax_filter_drops_zero_and_negative(patched_pv_dir):
    """Units with Pmax <= 0 must not appear in plant_records or avail."""
    units = [
        {"GenName": "good",  "Pmax": 5.0,  "Technology": "PV-roof"},
        {"GenName": "zero",  "Pmax": 0.0,  "Technology": "PV-roof"},
        {"GenName": "neg",   "Pmax": -1.0, "Technology": "PV-roof"},
    ]
    profiles = [
        _profile_row("good", 2050, 1.0),
        _profile_row("zero", 2050, 1.0),
        _profile_row("neg",  2050, 1.0),
    ]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, avail = hires.load_high_res_PV(DATASET, 2009, 2050)

    assert set(plant_records.index) == {"CH00_good_pvrf"}
    plants_in_avail = {pid for (pid, _t) in avail}
    assert plants_in_avail == {"CH00_good_pvrf"}


def test_orphan_profile_dropped_and_logged(patched_pv_dir, capsys):
    """A profile whose Name has no matching unit must be silently dropped
    from the result, and the dropped-count log line emitted."""
    units = [
        {"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"},
    ]
    profiles = [
        _profile_row("alpha",  2050, 2.0),
        _profile_row("ghost",  2050, 1.0),  # orphan: no matching unit
    ]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, avail = hires.load_high_res_PV(DATASET, 2009, 2050)

    assert set(plant_records.index) == {"CH00_alpha_pvrf"}
    plants_in_avail = {pid for (pid, _t) in avail}
    assert "CH00_ghost_pvrf" not in plants_in_avail

    captured = capsys.readouterr().out
    assert "[high_res_PV] dropped" in captured
    assert "0 unit(s) without profile" in captured
    assert "1 profile(s) without unit" in captured


def test_orphan_unit_dropped_and_logged(patched_pv_dir, capsys):
    """A unit with no matching profile row must be dropped from the result,
    and the dropped-count log line emitted."""
    units = [
        {"GenName": "alpha",   "Pmax": 4.0, "Technology": "PV-roof"},
        {"GenName": "lonely",  "Pmax": 3.0, "Technology": "PV-roof"},
    ]
    profiles = [
        _profile_row("alpha", 2050, 2.0),
    ]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, _ = hires.load_high_res_PV(DATASET, 2009, 2050)

    assert set(plant_records.index) == {"CH00_alpha_pvrf"}

    captured = capsys.readouterr().out
    assert "[high_res_PV] dropped" in captured
    assert "1 unit(s) without profile" in captured
    assert "0 profile(s) without unit" in captured


def test_exact_year_match_no_fallback_log(patched_pv_dir, capsys):
    """When run_year is present in the profile CSV, no fallback line is printed."""
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, _ = hires.load_high_res_PV(DATASET, 2009, 2050)

    assert len(plant_records) == 1
    captured = capsys.readouterr().out
    assert "not present in profile CSV" not in captured
    assert "using closest available year" not in captured
    # the load-summary line correctly reports the exact year
    assert "profile_year=2050" in captured


def test_year_fallback_picks_closest_and_logs(patched_pv_dir, capsys):
    """When run_year is missing, fall back to the closest year and log it."""
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    # Only 2050 available; request 2035 -> fallback to 2050
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, avail = hires.load_high_res_PV(DATASET, 2009, 2035)

    assert len(plant_records) == 1
    assert avail[("CH00_alpha_pvrf", "t_1")] == pytest.approx(0.5)

    captured = capsys.readouterr().out
    assert "requested run_year=2035 not present in profile CSV" in captured
    assert "using closest available year=2050" in captured
    # load-summary now reflects the resolved year
    assert "profile_year=2050" in captured


def test_missing_weather_year_folder_raises(patched_pv_dir):
    """Calling with a weather_year whose folder does not exist must raise
    FileNotFoundError listing the available folders."""
    # Only create the 2009 folder; request 1995
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    with pytest.raises(FileNotFoundError) as exc_info:
        hires.load_high_res_PV(DATASET, 1995, 2050)

    msg = str(exc_info.value)
    assert "weather_year=1995" in msg
    # The available-folders list should mention 2009
    assert "2009" in msg


def test_loader_caches_repeated_calls(patched_pv_dir):
    """A second call with the same args must hit the lru_cache and not
    re-parse the input files."""
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    # First call: miss
    hires.load_high_res_PV(DATASET, 2009, 2050)
    info_after_first = hires.load_high_res_PV.cache_info()
    assert info_after_first.misses == 1
    assert info_after_first.hits == 0

    # Second call with same args: must be a cache hit
    hires.load_high_res_PV(DATASET, 2009, 2050)
    info_after_second = hires.load_high_res_PV.cache_info()
    assert info_after_second.misses == 1
    assert info_after_second.hits == 1


def test_schema_compatibility_with_candidates_csv(patched_pv_dir):
    """The returned DataFrame must concat cleanly with
    plants_invest_candidates_res_CH.csv: same columns in the same order."""
    units = [
        {"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"},
    ]
    profiles = [_profile_row("alpha", 2050, 1.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    plant_records, _ = hires.load_high_res_PV(DATASET, 2009, 2050)

    candidates = pd.read_csv(CANDIDATES_CSV, header=0, index_col=0)

    # Same columns in same order
    assert list(plant_records.columns) == list(candidates.columns)

    # Concatenation does not introduce any new columns (i.e. schemas align)
    combined = pd.concat([candidates, plant_records])
    assert list(combined.columns) == list(candidates.columns)
    assert "CH00_alpha_pvrf" in combined.index


def test_missing_dataset_folder_raises_listing_datasets(patched_pv_dir):
    """A dataset name without a folder must raise and name the datasets present."""
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009, dataset="present")

    with pytest.raises(FileNotFoundError) as exc_info:
        hires.load_high_res_PV("absent", 2009, 2050)

    msg = str(exc_info.value)
    assert "'absent'" in msg
    assert "present" in msg


@pytest.mark.parametrize("dataset", [None, ""])
def test_unset_dataset_raises_value_error(patched_pv_dir, dataset):
    """The flag on without a dataset name is a configuration error, not a default."""
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    with pytest.raises(ValueError) as exc_info:
        hires.load_high_res_PV(dataset, 2009, 2050)

    assert "high_resolution_PV_dataset" in str(exc_info.value)
    assert DATASET in str(exc_info.value)


def test_units_csv_missing_column_raises(patched_pv_dir):
    """units.csv without one of GenName, Pmax, Technology must raise a clear error."""
    units = [{"GenName": "alpha", "Pmax": 4.0}]          # no Technology column
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)

    with pytest.raises(ValueError) as exc_info:
        hires.load_high_res_PV(DATASET, 2009, 2050)

    assert "Technology" in str(exc_info.value)
    assert "units.csv" in str(exc_info.value)


def test_missing_units_csv_raises(patched_pv_dir):
    """A dataset folder without units.csv must raise FileNotFoundError naming the file."""
    units = [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}]
    profiles = [_profile_row("alpha", 2050, 2.0)]
    _write_fixture(patched_pv_dir, units, profiles, weather_year=2009)
    (patched_pv_dir / DATASET / "2009" / "units.csv").unlink()

    with pytest.raises(FileNotFoundError) as exc_info:
        hires.load_high_res_PV(DATASET, 2009, 2050)

    assert "units.csv" in str(exc_info.value)


# --- cost classes -----------------------------------------------------------------

def test_load_cost_classes_reads_the_column_of_the_pv_roof_units_only(patched_pv_dir):
    units = [
        {"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof", "cost_class": "1-10kW"},
        {"GenName": "big-one", "Pmax": 9.0, "Technology": "PV-roof", "cost_class": " all "},
        {"GenName": "zero", "Pmax": 0.0, "Technology": "PV-roof", "cost_class": "all"},   # Pmax 0: no candidate
        {"GenName": "windy", "Pmax": 5.0, "Technology": "Wind", "cost_class": ""},        # not PV-roof
    ]
    _write_fixture(patched_pv_dir, units, [_profile_row("alpha", 2050, 1.0)])

    assert hires.load_cost_classes(DATASET, 2009) == {"CH00_alpha_pvrf": "1-10kW", "CH00_big_one_pvrf": "all"}


def test_load_cost_classes_is_none_when_units_csv_has_no_column(patched_pv_dir):
    _write_fixture(patched_pv_dir, [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof"}],
                   [_profile_row("alpha", 2050, 1.0)])
    assert hires.load_cost_classes(DATASET, 2009) is None


def test_load_cost_classes_rejects_a_blank_class(patched_pv_dir):
    units = [
        {"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof", "cost_class": "1-10kW"},
        {"GenName": "beta", "Pmax": 2.0, "Technology": "PV-roof", "cost_class": ""},
        {"GenName": "gamma", "Pmax": 2.0, "Technology": "PV-roof", "cost_class": None},
    ]
    _write_fixture(patched_pv_dir, units, [_profile_row("alpha", 2050, 1.0)])
    with pytest.raises(ValueError, match=r"2 PV-roof unit\(s\) of dataset 'ds' have an empty cost_class.*beta"):
        hires.load_cost_classes(DATASET, 2009)


def test_load_cost_classes_is_cached(patched_pv_dir):
    _write_fixture(patched_pv_dir, [{"GenName": "alpha", "Pmax": 4.0, "Technology": "PV-roof", "cost_class": "all"}],
                   [_profile_row("alpha", 2050, 1.0)])
    hires.load_cost_classes(DATASET, 2009)
    hires.load_cost_classes(DATASET, 2009)
    assert hires.load_cost_classes.cache_info().hits == 1


# --- replace_aggregated_pv_candidates ---------------------------------------------

def test_replace_aggregated_pv_candidates_drops_only_the_regional_pvrf_rows():
    candidates = pd.DataFrame(
        {"tech": ["pvrf", "windon", "pvrf", "pvrf", "pvrf"], "gen_max_limit": [1e4, 600.0, 1e4, 2e4, 1e4]},
        index=["CH01_pvrf", "CH01_windon", "CH07_pvrf", "DE01_pvrf", "CH01_pvrf"],   # the duplicate row the CSV has
    )
    per_plant = pd.DataFrame({"tech": ["pvrf"], "gen_max_limit": [3.0]}, index=["CH00_unit_pvrf"])

    out, n_removed = hires.replace_aggregated_pv_candidates(candidates, per_plant)

    assert n_removed == 3
    assert list(out.index) == ["CH01_windon", "DE01_pvrf", "CH00_unit_pvrf"]
    assert list(out.columns) == ["tech", "gen_max_limit"]
