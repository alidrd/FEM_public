"""Unit tests for the neighbour prices that read_line_data (model/data_import_fcns.py) loads in CH_only mode.

A CH_only run prices every hour's trade on each CH00 border line at the neighbour's price, so a missing
setting, file, column, time step or finite value must stop the run; zero and negative prices are valid.
The tests run the production reader on small input files in a temporary working directory.
"""
import pandas as pd
import pytest

from model.data_import_fcns import read_line_data

T_LIST = ["t_1", "t_2"]
NEIGHBOURS = ["DE00", "AT00", "FR00", "IT00"]


@pytest.fixture
def folder(tmp_path, monkeypatch):
    """NTC inputs with the four CH00 borders and one line away from CH00; the price folder is empty."""
    ntc = tmp_path / "input" / "NTC"
    ntc.mkdir(parents=True)
    lines = pd.Index([f"{n}-CH00" for n in NEIGHBOURS] + ["DE00-FR00"], name="index")
    pd.DataFrame({"Import Capacity (MW)": 1000.0}, index=lines).to_csv(ntc / "NTC_import_TEST_2050.csv")
    pd.DataFrame({"Export Capacity (MW)": 800.0}, index=lines).to_csv(ntc / "NTC_export_TEST_2050.csv")
    (tmp_path / "input" / "neighbour_prices_for_CH_only_mode").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def write_prices(folder, rows):
    path = folder / "input" / "neighbour_prices_for_CH_only_mode" / "neighbor_prices_test.csv"
    pd.DataFrame(rows).to_csv(path, index=False)


def price_rows(overrides=None):
    """One row per time step, every neighbour at 50 EUR/MWh; overrides map (time step, neighbour) to a value."""
    rows = [{"time_step": t, **{n: 50.0 for n in NEIGHBOURS}} for t in T_LIST]
    for (t, n), value in (overrides or {}).items():
        rows[T_LIST.index(t)][n] = value
    return rows


def read_prices(**settings):
    arguments = dict(year=2050, Node_list=["CH00"], T_list=T_LIST, NTC_CH_ratio=1, eu_policy="TEST", CH_only=True,
                     scenario_name="s", neighbor_countries=NEIGHBOURS, neighbor_price_scenario="test")
    arguments.update(settings)
    return read_line_data(**arguments)[4]


def test_every_border_line_gets_every_step_with_zero_and_negative_kept(folder):
    write_prices(folder, price_rows({("t_1", "DE00"): 0.0, ("t_2", "IT00"): -12.5}))
    prices = read_prices()
    assert len(prices) == 4 * len(T_LIST)
    assert prices[("HVAC_DE00_CH00", "t_1")] == 0.0
    assert prices[("HVAC_IT00_CH00", "t_2")] == -12.5
    assert prices[("HVAC_FR00_CH00", "t_2")] == 50.0


def test_missing_file_raises(folder):
    with pytest.raises(FileNotFoundError, match="neighbor_prices_test.csv"):
        read_prices()


def test_missing_neighbour_column_raises(folder):
    rows = price_rows()
    for row in rows:
        del row["FR00"]
    write_prices(folder, rows)
    with pytest.raises(ValueError, match="FR00"):
        read_prices()


def test_missing_time_step_raises(folder):
    write_prices(folder, price_rows()[:1])
    with pytest.raises(ValueError, match="t_2"):
        read_prices()


def test_duplicate_time_step_raises(folder):
    rows = price_rows()
    write_prices(folder, rows + rows[:1])
    with pytest.raises(ValueError, match="duplicate"):
        read_prices()


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_non_finite_price_raises(folder, value):
    write_prices(folder, price_rows({("t_2", "AT00"): value}))
    with pytest.raises(ValueError, match="AT00"):
        read_prices()


@pytest.mark.parametrize("setting, value", [
    ("scenario_name", None),
    ("neighbor_countries", []),
    ("neighbor_price_scenario", None),
])
def test_empty_setting_stops_the_run(folder, setting, value):
    write_prices(folder, price_rows())
    with pytest.raises(ValueError, match="settings are empty"):
        read_prices(**{setting: value})


def test_border_neighbour_missing_from_neighbour_list_raises(folder):
    write_prices(folder, price_rows())
    with pytest.raises(ValueError, match="IT00"):
        read_prices(neighbor_countries=["DE00", "AT00", "FR00"])


def test_run_that_is_not_ch_only_reads_no_prices(folder):
    prices = read_prices(CH_only=False, Node_list=["DE00", "FR00"], neighbor_price_scenario=None)
    assert prices == {}
