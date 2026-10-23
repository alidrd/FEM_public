"""Unit tests for the reservoir inflow of a back-cast and its electrical basis: read_inflow, spread_weeks,
inflow_shares and the efficiencies of read_hydro_fleet in data_prep/historical_import.py. The efficiency tests check
the production storage balance on small Pyomo models (no solver).
"""
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
import pytest

import input.cost_operation_invest_data as operation_data
import model.components_common as cc
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model.constraint_scaling import constraint_scaling
from tests.backcast_data import write_processed

NAME = "hist_inflow_obs"
# 2025-09-30 22:00 to 2025-10-12 22:00 UTC: 122 hours of the week from Monday 29 September, then 166 of the next
TWO_WEEKS = "2025-10-01/2025-10-12"
HYDRO = ["CH00_dam", "CH00_psp_close", "large_psp", "medium_reservior", "small_reservior"]
SHARES = {"large_psp": 0.716, "medium_reservior": 0.245, "small_reservior": 0.039}
TOLERANCE = 1e-9
ROUND_TRIP = 0.87 * 0.87  # the open plants' charging efficiency on the electrical basis


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(period=TWO_WEEKS, **changes):
        folder = write_processed(tmp_path / period.replace("/", "_"), period)
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": period,
            "historical_data_dir": str(folder), **changes})
        return read_settings.read_scenario_settings(NAME)
    return make


def rewrite(settings, change):
    path = Path(settings["historical_data_dir"]) / hi.PECD_TABLE
    change(pd.read_csv(path)).to_csv(path, index=False)


def week_energy(settings, series, start):
    table = pd.read_csv(Path(settings["historical_data_dir"]) / hi.PECD_TABLE)
    row = table[(table["series"] == series) & (table["week_start_utc"].str.startswith(start))]
    return float(row["energy_MWh"].iloc[0])


def total_inflow(staged):
    return pd.Series(staged["Inflow_data"]).groupby(level=1).sum()


# --- shares -------------------------------------------------------------------------------------------------------

def shares_table(tmp_path, monkeypatch, rows):
    path = tmp_path / "representative.csv"
    pd.DataFrame(rows, columns=["name", "variable", "inflow_share"]).to_csv(path, index=False)
    monkeypatch.setattr(hi, "REPRESENTATIVE_TABLE", str(path))


def test_one_share_per_plant_from_its_repeated_rows(tmp_path, monkeypatch):
    assert hi.inflow_shares() == SHARES
    shares_table(tmp_path, monkeypatch, [("a", "gen_cap_MW", 0.6), ("a", "sto_GWh", 0.6), ("b", "gen_cap_MW", 0.4)])
    assert hi.inflow_shares() == {"a": 0.6, "b": 0.4}


@pytest.mark.parametrize("rows, message", [
    ([("a", "gen_cap_MW", 0.6), ("a", "sto_GWh", 0.5), ("b", "gen_cap_MW", 0.4)], r"rows of a disagree on inflow_share"),
    ([("a", "gen_cap_MW", 0.6), ("b", "gen_cap_MW", 0.3)], "sum to 0.9, not 1"),
])
def test_disagreeing_rows_or_shares_not_summing_to_one_raise(tmp_path, monkeypatch, rows, message):
    shares_table(tmp_path, monkeypatch, rows)
    with pytest.raises(ValueError, match=message):
        hi.inflow_shares()


# --- weeks --------------------------------------------------------------------------------------------------------

def test_partial_first_and_last_weeks_contribute_only_their_overlap(settings_for):
    settings = settings_for()
    staged, _, assumptions = hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)
    first = week_energy(settings, "HRI", "2025-09-29") + week_energy(settings, "HOL", "2025-09-29")
    last = week_energy(settings, "HRI", "2025-10-06") + week_energy(settings, "HOL", "2025-10-06")
    total = total_inflow(staged)
    assert len(total) == 122 + 166
    assert total.sum() == pytest.approx(first * 122 / 168 + last * 166 / 168, abs=TOLERANCE)
    assert "{'HRI': (122, 166), 'HOL': (122, 166)}" in assumptions[1]


def test_a_week_across_the_change_to_summer_time_keeps_its_168_hours(settings_for):
    settings = settings_for("2025-03-23/2025-03-31")  # the clocks change on Sunday 30 March
    staged = hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)[0]
    time_map = hi.historical_time_map(settings["historical_period"]).set_index("t")
    utc = time_map.loc[total_inflow(staged).index, "time_utc"]
    in_week = ((utc >= pd.Timestamp("2025-03-24", tz="UTC")) & (utc < pd.Timestamp("2025-03-31", tz="UTC"))).to_numpy()
    expected = week_energy(settings, "HRI", "2025-03-24") + week_energy(settings, "HOL", "2025-03-24")
    assert in_week.sum() == 168 and total_inflow(staged)[in_week].sum() == pytest.approx(expected, abs=TOLERANCE)


def test_energy_is_conserved_across_plants_and_other_plants_get_zero(settings_for):
    settings = settings_for()
    staged = hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)[0]
    inflow = pd.Series(staged["Inflow_data"])
    hourly = hi.read_inflow(NAME, settings, [], ROUND_TRIP)[0]
    assert set(staged["Plant_inflow_list"]) == set(HYDRO) and set(hourly["Plant_inflow_list"]) == set(SHARES)
    by_plant = inflow.groupby(level=0).sum()
    assert by_plant["CH00_dam"] == by_plant["CH00_psp_close"] == 0.0
    assert by_plant[list(SHARES)].sum() == pytest.approx(total_inflow(hourly).sum(), abs=TOLERANCE)
    t = settings["T_list"][5]
    assert inflow[("large_psp", t, NAME)] == pytest.approx(0.716 * total_inflow(hourly)[t], abs=TOLERANCE)


@pytest.mark.parametrize("change, message", [
    (lambda f: pd.concat([f, f.iloc[[2]].assign(week_start_utc=lambda g: (pd.to_datetime(g["week_start_utc"])
                                                                           + pd.Timedelta(hours=24)).astype(str),
                                                week_end_utc=lambda g: (pd.to_datetime(g["week_end_utc"])
                                                                         + pd.Timedelta(hours=24)).astype(str))]),
     "HRI weeks overlap"),
    (lambda f: f.assign(energy_MWh=f["energy_MWh"].where(f.index != 2, float("inf"))), "HRI weeks with missing"),
    (lambda f: f.assign(energy_MWh=f["energy_MWh"].where(f.index != 2)), "HRI weeks with missing"),
    (lambda f: f.assign(energy_MWh=f["energy_MWh"].where(f.index != 2, -1.0)), "or negative energy"),
    (lambda f: f.drop(index=2), "no HRI week covers 166 hours of the period"),
    (lambda f: f.assign(week_end_utc=f["week_end_utc"].where(f.index != 1, f["week_start_utc"])), "is not 168 hours"),
])
def test_invalid_weeks_raise(settings_for, change, message):
    settings = settings_for()
    rewrite(settings, change)
    with pytest.raises(ValueError, match=message):
        hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)


def test_a_missing_value_outside_the_period_is_not_read(settings_for):
    settings = settings_for()
    rewrite(settings, lambda f: f.assign(energy_MWh=f["energy_MWh"].where(f.index != 0)))  # the week before
    assert len(hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)[0]["Inflow_data"]) == len(HYDRO) * 288


def test_assumptions_record_the_series_and_the_split(settings_for):
    staged, files, assumptions = hi.read_inflow(NAME, settings_for(), HYDRO, ROUND_TRIP)
    assert "HRI + HOL" in assumptions[0] and "weeks by release {'HRI': {'COM': 2}, 'HOL': {'COM': 2}}" in assumptions[0]
    assert "{'large_psp': 0.716, 'medium_reservior': 0.245, 'small_reservior': 0.039}" in assumptions[2]
    assert [Path(f).name for f in files] == [hi.PECD_TABLE, Path(hi.REPRESENTATIVE_TABLE).name]


# --- monthly level from the BFE mass balance ------------------------------------------------------------------------

TWO_MONTHS = "2025-10-01/2025-11-30"
BALANCE = {(2025, 10): (1800.0, 270.0), (2025, 11): (1850.0, 260.0)}  # storage generation, pumping (GWh)


def write_balance(tmp_path, monkeypatch, balance=BALANCE, columns=("Erzeugung_Speicherwerk_GWh",
                                                                    "Verbrauch_Speicherpumpen_GWh")):
    """The BFE monthly balance in its published layout, a notice line before and after the table."""
    lines = ["Diese Daten werden nicht mehr weitergeführt.", ",".join(["Jahr", "Monat", "Definitiv", *columns])]
    lines += [f"{y},{m},0,{g},{p}" for (y, m), (g, p) in balance.items()]
    lines.append("Diese Daten werden nicht mehr weitergeführt.")
    path = tmp_path / "ogd35.csv"
    path.write_bytes(("\n".join(lines) + "\n").encode("latin-1"))
    monkeypatch.setattr(hi, "BFE_BALANCE", str(path))
    return path


def stock_GWh(settings, instant):
    """National BFE stock at a local instant, linear between the Monday readings (Sunday 24:00 local)."""
    table = pd.read_csv(Path(settings["historical_data_dir"]) / hi.RESERVOIR_TABLE)
    readings = pd.Series(table["TotalCH_GWh"].to_numpy(),
                         index=pd.to_datetime(table["date"]).dt.tz_localize("Europe/Zurich"))
    instant = pd.Timestamp(instant, tz="Europe/Zurich")
    before, after = readings[readings.index <= instant], readings[readings.index >= instant]
    t0, t1 = before.index[-1], after.index[0]
    return before.iloc[-1] if t0 == t1 else before.iloc[-1] + (after.iloc[0] - before.iloc[-1]) * ((instant - t0) / (t1 - t0))


def month_of_steps(settings):
    time_map = hi.historical_time_map(settings["historical_period"])
    return pd.Series(time_map["time_local"].dt.strftime("%Y-%m").to_numpy(), index=time_map["t"])


def test_each_month_carries_the_bfe_mass_balance_with_the_pecd_shape_inside(settings_for, tmp_path, monkeypatch):
    path = write_balance(tmp_path, monkeypatch)
    settings = settings_for(TWO_MONTHS, historical_inflow_level="bfe_mass_balance")
    staged, files, assumptions = hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)
    pecd = total_inflow(hi.read_inflow(NAME, settings_for(TWO_MONTHS), HYDRO, ROUND_TRIP)[0])
    total = total_inflow(staged)
    month = month_of_steps(settings).reindex(total.index)
    starts = {"2025-10": ("2025-10-01", "2025-11-01"), "2025-11": ("2025-11-01", "2025-12-01")}
    for (y, m), (generation, pumping) in BALANCE.items():
        key = f"{y}-{m:02d}"
        change = stock_GWh(settings, starts[key][1]) - stock_GWh(settings, starts[key][0])
        expected = 1000 * (change + generation - ROUND_TRIP * pumping)
        assert total[month == key].sum() == pytest.approx(expected, rel=TOLERANCE)
        ratio = total[month == key] / pecd[month == key]
        assert ratio.max() - ratio.min() <= TOLERANCE * ratio.mean()  # the PECD shape within the month
    assert str(path) in files
    assert any(a.startswith("monthly level: BFE mass balance") for a in assumptions)
    assert any("2 lines of the BFE file are not data and were left out" in a for a in assumptions)


@pytest.mark.parametrize("period, balance, columns, level, message", [
    ("2025-10-01/2025-11-15", BALANCE, None, "bfe_mass_balance", "needs a period of whole local months"),
    (TWO_MONTHS, {(2025, 10): (1800.0, 270.0)}, None, "bfe_mass_balance", "no BFE balance for 2025-11"),
    (TWO_MONTHS, {(2025, 10): (1800.0, 270.0), (2025, 11): (10.0, 5000.0)}, None, "bfe_mass_balance",
     "negative natural inflow in 2025-11"),
    (TWO_MONTHS, BALANCE, ("Erzeugung_Speicherwerk_GWh", "Pumpen_GWh"), "bfe_mass_balance",
     r"lacks the columns \['Verbrauch_Speicherpumpen_GWh'\]"),
    (TWO_MONTHS, BALANCE, None, "bfe", "historical_inflow_level must be 'pecd' or 'bfe_mass_balance'"),
])
def test_invalid_mass_balance_inputs_raise(settings_for, tmp_path, monkeypatch, period, balance, columns, level,
                                           message):
    write_balance(tmp_path, monkeypatch, balance, *([columns] if columns else []))
    settings = settings_for(period, historical_inflow_level=level)
    with pytest.raises(ValueError, match=message):
        hi.read_inflow(NAME, settings, HYDRO, ROUND_TRIP)


def test_reservoir_inflow_is_no_longer_missing():
    assert "reservoir inflow" not in hi.NOT_YET_BUILT


# --- electrical basis ---------------------------------------------------------------------------------------------

def balance_model(eff_in, eff_out):
    """One storage plant over two cyclic model steps with the production storage balance."""
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=["large_psp"])
    for name in ("P_storage", "P_pumping", "P_gen", "P_inflow", "P_hydro"):
        m.add_component(name, pyo.Set(initialize=["large_psp"]))
    m.P_outflow = pyo.Set(initialize=[])
    m.T = pyo.Set(initialize=["t_1", "t_2"], ordered=True)
    m.Scenarios = pyo.Set(initialize=["s"])
    m.ConstraintNames = pyo.Set(initialize=["storage_soc"])
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=constraint_scaling["storage_soc"])
    m.storage_charge_eff_in = pyo.Param(m.P_storage, m.Scenarios, initialize=eff_in)
    m.storage_charge_eff_out = pyo.Param(m.P_storage, m.Scenarios, initialize=eff_out)
    m.inflow = pyo.Param(m.P_inflow, m.T, m.Scenarios, mutable=True, initialize=0.0)
    for name in ("soc", "storage_charge", "gen", "spill_water"):
        m.add_component(name, pyo.Var(m.P, m.T, m.Scenarios, initialize=0.0))
    m.storage_soc = pyo.Constraint(m.P_storage, m.T, m.Scenarios, rule=cc.storeBalance_Constraint)
    return m


def balance_holds(m, generation):
    """Unchanged stock (cyclic), no spill: does the balance hold with this generation in the second step?"""
    m.soc["large_psp", "t_2", "s"].value = 1000.0
    m.gen["large_psp", "t_2", "s"].value = generation
    charged = m.storage_charge["large_psp", "t_1", "s"].value * m.storage_charge_eff_in["large_psp", "s"]
    m.soc["large_psp", "t_1", "s"].value = 1000.0 + charged + pyo.value(m.inflow["large_psp", "t_1", "s"])
    rows = [m.storage_soc["large_psp", t, "s"] for t in m.T]
    return all(abs(pyo.value(row.body) - pyo.value(row.upper)) <= TOLERANCE for row in rows)


def fleet_efficiencies(settings):
    fleet = hi.read_hydro_fleet(NAME, settings)[0]
    return fleet["Map_eff_in_plant"]["large_psp"], fleet["Map_eff_out_plant"]["large_psp"]


def test_100_MWh_of_inflow_allow_100_MWh_of_generation_and_87_with_the_old_efficiency(settings_for):
    eff_in, eff_out = fleet_efficiencies(settings_for())
    workbook = hi.year_independent("efficiency", ["psp_open"])["psp_open"]
    for (charge_eff, discharge_eff), allowed in (((eff_in, eff_out), 100.0), ((workbook, workbook), 87.0)):
        m = balance_model(charge_eff, discharge_eff)
        m.inflow["large_psp", "t_1", "s"] = 100.0
        assert balance_holds(m, allowed) and not balance_holds(m, allowed + 0.01) and not balance_holds(m, allowed - 0.01)


@pytest.mark.parametrize("workbook_in, workbook_out, allowed", [(0.87, 0.87, 75.69), (0.8, 0.9, 72.0)])
def test_pump_cycle_returns_the_workbook_round_trip(settings_for, monkeypatch, workbook_in, workbook_out, allowed):
    monkeypatch.setitem(operation_data.cost_component["efficiency_into_storage"], "psp_open",
                        {year: workbook_in for year in hi.WORKBOOK_YEARS})
    monkeypatch.setitem(operation_data.cost_component["efficiency"], "psp_open",
                        {year: workbook_out for year in hi.WORKBOOK_YEARS})
    eff_in, eff_out = fleet_efficiencies(settings_for())
    assert eff_out == 1.0
    m = balance_model(eff_in, eff_out)
    m.storage_charge["large_psp", "t_1", "s"].value = 100.0
    assert balance_holds(m, allowed) and not balance_holds(m, allowed + 0.01) and not balance_holds(m, allowed - 0.01)


def test_a_plant_with_an_inflow_share_outside_the_hydro_fleet_raises(settings_for, tmp_path, monkeypatch):
    shares_table(tmp_path, monkeypatch, [("large_psp", "sto_GWh", 0.9), ("CH05_reservoir", "sto_GWh", 0.1)])
    with pytest.raises(ValueError, match=r"open plants \['CH05_reservoir'\] are not in the hydro fleet"):
        hi.read_hydro_fleet(NAME, settings_for())


def test_closed_loop_and_dam_keep_the_workbook_efficiencies(settings_for):
    fleet = hi.read_hydro_fleet(NAME, settings_for())[0]
    for plant, tech in (("CH00_psp_close", "psp_close"), ("CH00_dam", "dam")):
        assert fleet["Map_eff_in_plant"][plant] == hi.year_independent("efficiency_into_storage", [tech])[tech]
        assert fleet["Map_eff_out_plant"][plant] == hi.year_independent("efficiency", [tech])[tech]


@pytest.mark.parametrize("fleet_name", ["representative_2030", "wasta_individual"])
def test_pump_round_trip_is_the_same_for_every_plant_with_pumps(settings_for, fleet_name):
    settings = settings_for(historical_hydro_fleet=fleet_name)
    fleet = hi.read_hydro_fleet(NAME, settings)[0]
    assert hi.pump_round_trip(fleet, NAME) == pytest.approx(ROUND_TRIP, rel=TOLERANCE)


def test_pump_round_trips_that_differ_raise(settings_for):
    fleet = hi.read_hydro_fleet(NAME, settings_for())[0]
    fleet["Map_eff_out_plant"] = {**fleet["Map_eff_out_plant"], "CH00_psp_close": 0.9}
    with pytest.raises(ValueError, match="disagree on their round trip"):
        hi.pump_round_trip(fleet, NAME)


def test_the_run_record_takes_the_bfe_columns_from_its_header_line(tmp_path, monkeypatch):
    path = write_balance(tmp_path, monkeypatch)
    record = hi.file_record(str(path))
    assert record["columns"] == ["Jahr", "Monat", "Definitiv", "Erzeugung_Speicherwerk_GWh",
                                 "Verbrauch_Speicherpumpen_GWh"]
