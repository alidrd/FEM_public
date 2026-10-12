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
# large_psp is Swiss although its name lacks "CH"; DE00_gas and DE01_pvrf are foreign
PLANTS = {"CH00": ["CH00_dam", "large_psp"], "DE00": ["DE00_gas"]}
INFEED_NODES = {"CH01_pvrf": "CH00", "DE01_pvrf": "DE00"}
SWISS = ["CH00_dam", "CH01_pvrf", "large_psp"]


def export(tmp_path, monkeypatch, empty=(), infeed_nodes=INFEED_NODES, countries=None):
    """Export the five components with par_var, 100, 200, ... per entry (infeed plants for gen_max_infeedp, plants
    for the others; none for those in empty), and the maps of plants, infeed plants and nodes."""
    monkeypatch.chdir(tmp_path)
    m = pyo.ConcreteModel()
    m.Node = pyo.Set(initialize=list(PLANTS))
    m.P = pyo.Set(initialize=[p for plants in PLANTS.values() for p in plants])
    m.Infeedp = pyo.Set(initialize=list(INFEED_NODES))
    m.Empty = pyo.Set(initialize=[])
    m.Scenarios = pyo.Set(initialize=["s"])
    for k, name in enumerate(COLUMNS):
        index = m.Empty if name in empty else m.Infeedp if name == "gen_max_infeedp" else m.P
        m.add_component(name, pyo.Var(index, m.Scenarios, initialize=100.0 * (k + 1)))
    m.Map_node_plant = pyo.Param(m.Node, initialize=PLANTS, within=pyo.Any)
    m.InfeedMapped = pyo.Set(initialize=list(infeed_nodes))
    m.Map_infeedplant_node = pyo.Param(m.InfeedMapped, initialize=infeed_nodes, within=pyo.Any)
    countries = countries or {"CH00": "CH", "DE00": "DE"}
    m.CountryNodes = pyo.Set(initialize=list(countries))
    m.Map_node_country = pyo.Param(m.CountryNodes, initialize=countries, within=pyo.Any)
    par_var([m.component(name) for name in [*COLUMNS, "Map_node_plant", "Map_infeedplant_node", "Map_node_country"]],
            "run")
    return tmp_path / "output" / "run"


def summary(folder):
    investment_summary(f"{folder}/")
    return pd.read_csv(folder / "investment_summary.csv", index_col=0)


def test_summary_holds_the_entries_at_swiss_nodes_whatever_their_name(tmp_path, monkeypatch):
    table = summary(export(tmp_path, monkeypatch))
    assert list(table.index) == SWISS
    assert table.at["CH01_pvrf", "Existing Power (MW)"] == 100.0
    assert [table.at["large_psp", column] for name, column in COLUMNS.items() if name != "gen_max_infeedp"] == [
        200.0, 300.0, 400.0, 500.0]


@pytest.mark.parametrize("empty", list(COLUMNS))
def test_an_empty_export_gives_no_rows(tmp_path, monkeypatch, empty):
    folder = export(tmp_path, monkeypatch, empty=[empty])
    assert (folder / f"{empty}.csv").read_text().strip() == '""'  # how par_var writes a component without entries
    table = summary(folder)
    assert all(pd.isna(table.at[row, COLUMNS[empty]]) for row in ["CH00_dam", "large_psp"])
    assert all(table.at["large_psp", column] > 0 for name, column in COLUMNS.items()
               if name not in (empty, "gen_max_infeedp"))


def test_a_run_without_infeed_plants_gives_the_swiss_plants(tmp_path, monkeypatch):
    folder = export(tmp_path, monkeypatch, empty=["gen_max_infeedp"], infeed_nodes={})  # as in a back-cast
    assert (folder / "Map_infeedplant_node.csv").read_text().strip() == '""'
    assert list(summary(folder).index) == ["CH00_dam", "large_psp"]


def test_a_file_with_rows_but_too_few_columns_raises_naming_it(tmp_path, monkeypatch):
    folder = export(tmp_path, monkeypatch)
    (folder / "gen_max.csv").write_text("P,value\nCH00_dam,5\n")
    with pytest.raises(ValueError, match="gen_max.csv"):
        investment_summary(f"{folder}/")


def test_an_entry_neither_map_lists_raises_naming_it(tmp_path, monkeypatch):
    folder = export(tmp_path, monkeypatch)
    with open(folder / "gen_max.csv", "a") as handle:
        handle.write("XX99_unknown,s,5.0\n")
    with pytest.raises(ValueError, match=r"gen_max.csv: no node .* for 1 entries, e.g. \['XX99_unknown'\]"):
        investment_summary(f"{folder}/")


@pytest.mark.parametrize("infeed_nodes, countries, message", [
    ({**INFEED_NODES, "large_psp": "DE00"}, None, r"place \['large_psp'\] at different nodes"),
    (INFEED_NODES, {"CH00": "CH"}, r"no country for the nodes \['DE00'\]"),
])
def test_inconsistent_maps_raise(tmp_path, monkeypatch, infeed_nodes, countries, message):
    folder = export(tmp_path, monkeypatch, infeed_nodes=infeed_nodes, countries=countries)
    with pytest.raises(ValueError, match=message):
        investment_summary(f"{folder}/")
