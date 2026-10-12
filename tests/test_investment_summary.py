"""Unit tests for model/investment_summary.py on small exports written by the production exporter (par_var in
aggregation/results_export.py), which writes a component without entries as a file holding only "".
"""
import pandas as pd
import pyomo.environ as pyo
import pytest

from aggregation.results_export import par_var
from model.investment_summary import investment_summary

# The files the summary reads with usecols=[0, 2], and the column each fills
COLUMNS = {
    "gen_max_infeedp": "Existing Power (MW)",
    "gen_max": "Added Power (MW)",
    "gen_energy_max": "Storage level (MWh)",
    "investment_genmax_slp": "Investment Cost (CHF/MW)",
    "investment_emax_slp": "Storage Cost (CHF/MWh)",
}


def export(tmp_path, monkeypatch, empty=()):
    """Export the five components with par_var: 100, 200, ... for a Swiss and a German plant, or no entries."""
    monkeypatch.chdir(tmp_path)
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=["CH00_dam", "DE00_gas"])
    m.Empty = pyo.Set(initialize=[])
    m.Scenarios = pyo.Set(initialize=["s"])
    for k, name in enumerate(COLUMNS):
        m.add_component(name, pyo.Var(m.Empty if name in empty else m.P, m.Scenarios, initialize=100.0 * (k + 1)))
    par_var([m.component(name) for name in COLUMNS], "run")
    return tmp_path / "output" / "run"


def summary(folder):
    investment_summary(f"{folder}/")
    return pd.read_csv(folder / "investment_summary.csv", index_col=0)


def test_summary_holds_the_swiss_rows_of_every_component(tmp_path, monkeypatch):
    table = summary(export(tmp_path, monkeypatch))
    assert list(table.index) == ["CH00_dam"]
    assert [table.at["CH00_dam", column] for column in COLUMNS.values()] == [100.0, 200.0, 300.0, 400.0, 500.0]


@pytest.mark.parametrize("empty", list(COLUMNS))
def test_an_empty_export_gives_no_rows(tmp_path, monkeypatch, empty):
    folder = export(tmp_path, monkeypatch, empty=[empty])
    assert (folder / f"{empty}.csv").read_text().strip() == '""'  # how par_var writes a component without entries
    table = summary(folder)
    assert pd.isna(table.at["CH00_dam", COLUMNS[empty]])
    assert all(table.at["CH00_dam", column] > 0 for name, column in COLUMNS.items() if name != empty)


def test_a_file_with_rows_but_too_few_columns_raises_naming_it(tmp_path, monkeypatch):
    folder = export(tmp_path, monkeypatch)
    (folder / "gen_max.csv").write_text("P,value\nCH00_dam,5\n")
    with pytest.raises(ValueError, match="gen_max.csv"):
        investment_summary(f"{folder}/")
