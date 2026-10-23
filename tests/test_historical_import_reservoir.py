"""Unit tests for the reservoir boundaries of a back-cast: read_reservoir_stocks, fill_readings and fill_at in
data_prep/historical_import.py, and add_reservoir_boundaries in model/components_historical.py. The model tests use
the production storage balance and start condition on small Pyomo models, and the production dual exporter (no
solver).
"""
from pathlib import Path

import pandas as pd
import pyomo.environ as pyo
import pytest
from pyomo.repn import generate_standard_repn

import aggregation.results_export as res_export
import model.components_common as cc
import model.read_settings as read_settings
from data_prep import historical_import as hi
from model import components_historical as ch
from tests.backcast_data import write_processed

NAME = "hist_reservoir_obs"
# 2025-09-30 22:00 to 2025-10-05 22:00 UTC: the start lies 48 hours after the reading of Monday 29 September
# (2025-09-28 22:00 UTC), the end is the reading of Monday 6 October (2025-10-05 22:00 UTC)
PERIOD = "2025-10-01/2025-10-05"
OPEN_STORAGE = {"large_psp": 8647500.0, "medium_reservior": 151400.0, "small_reservior": 1200.0}
TOLERANCE = 1e-9


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(period=PERIOD, **changes):
        folder = write_processed(tmp_path / period.replace("/", "_"), period)
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": period,
            "historical_data_dir": str(folder), **changes})
        return read_settings.read_scenario_settings(NAME)
    return make


def fleet(settings):
    return hi.read_hydro_fleet(NAME, settings)[0]


def readings(settings):
    table = pd.read_csv(Path(settings["historical_data_dir"]) / hi.RESERVOIR_TABLE)
    return table.assign(fill=table["TotalCH_GWh"] / table["TotalCH_max_GWh"]).set_index("date")["fill"]


def rewrite(settings, change):
    path = Path(settings["historical_data_dir"]) / hi.RESERVOIR_TABLE
    change(pd.read_csv(path)).to_csv(path, index=False)


# --- data side ----------------------------------------------------------------------------------------------------

def test_monday_reading_is_sunday_24_local_in_utc():
    table = pd.DataFrame({"date": ["2025-09-29", "2025-12-01"], "TotalCH_GWh": [7600.0, 6000.0],
                          "TotalCH_max_GWh": 8895.0})
    instants = hi.fill_readings(table).index
    assert list(instants) == [pd.Timestamp("2025-09-28 22:00", tz="UTC"), pd.Timestamp("2025-11-30 23:00", tz="UTC")]


def test_start_stock_lies_before_the_first_hour_and_the_end_target_after_the_last(settings_for):
    settings = settings_for()
    staged, files, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    hours = hi.historical_time_map(PERIOD)["time_utc"]
    fill = readings(settings)
    start_fill = fill["2025-09-29"] + (fill["2025-10-06"] - fill["2025-09-29"]) * 48 / 168
    for p, storage in OPEN_STORAGE.items():  # equal fill fraction for every open plant
        assert staged["Reservoir_start_stock"][(p, NAME)] == pytest.approx(start_fill * storage, rel=TOLERANCE)
    assert staged["Reservoir_end_target"] == {NAME: {"mode": "equal", "MWh": {"TotalCH": pytest.approx(
        fill["2025-10-06"] * sum(OPEN_STORAGE.values()), rel=TOLERANCE)}}}
    assert staged["Reservoir_group"] == {p: "TotalCH" for p in OPEN_STORAGE}  # one national group
    assert f"start stock at {hours.iloc[0]} (before the first hour)" in assumptions[1]
    assert f"end target at {hours.iloc[-1] + pd.Timedelta(hours=1)} (after the last hour)" in assumptions[2]
    assert "reading at 2025-10-05 22:00:00+00:00" in assumptions[2]  # an exact reading is used directly
    assert [Path(f).name for f in files] == [hi.RESERVOIR_TABLE]


def test_start_and_end_at_the_same_offset_in_their_weeks_are_interpolated_separately(settings_for):
    settings = settings_for("2025-10-01/2025-10-07")  # both instants lie 48 hours after a reading
    staged = hi.read_reservoir_stocks(NAME, settings, fleet(settings))[0]
    fill = readings(settings)
    start = fill["2025-09-29"] + (fill["2025-10-06"] - fill["2025-09-29"]) * 48 / 168
    end = fill["2025-10-06"] + (fill["2025-10-13"] - fill["2025-10-06"]) * 48 / 168
    assert start != end
    assert staged["Reservoir_start_stock"][("large_psp", NAME)] == pytest.approx(start * 8647500.0, rel=TOLERANCE)
    assert staged["Reservoir_end_target"][NAME]["MWh"]["TotalCH"] == pytest.approx(end * sum(OPEN_STORAGE.values()),
                                                                              rel=TOLERANCE)


@pytest.mark.parametrize("change, message", [
    (lambda f: f[f["date"] != "2025-10-06"], "more than 8 days apart"),
    (lambda f: f.assign(TotalCH_max_GWh=f["TotalCH_max_GWh"].where(f["date"] != "2025-10-13", 0.0)),
     r"TotalCH_max_GWh is not positive on \['2025-10-13'\]"),
    (lambda f: f[f["date"] >= "2025-10-06"],
     "no reading before 2025-09-30 22:00:00\\+00:00; stocks are not extrapolated"),
    (lambda f: f[f["date"] <= "2025-09-29"], "no reading after"),
    (lambda f: pd.concat([f, f.iloc[[1]]]), "duplicate dates"),
])
def test_invalid_readings_raise(settings_for, change, message):
    settings = settings_for()
    rewrite(settings, change)
    with pytest.raises(ValueError, match=message):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


def test_at_least_keeps_the_end_target(settings_for):
    settings = settings_for(historical_hydro_end="at_least")
    staged = hi.read_reservoir_stocks(NAME, settings, fleet(settings))[0]
    assert staged["Reservoir_end_target"] == {NAME: {"mode": "at_least", "MWh": {"TotalCH": pytest.approx(
        readings(settings)["2025-10-06"] * sum(OPEN_STORAGE.values()), rel=TOLERANCE)}}}


@pytest.mark.parametrize("mode, raises", [("equal", True), ("free", False)])
def test_free_end_needs_no_reading_after_the_end(settings_for, mode, raises):
    settings = settings_for("2025-10-01/2025-10-08", historical_hydro_end=mode)  # ends between two readings
    rewrite(settings, lambda f: f[f["date"] <= "2025-10-06"])
    if raises:
        with pytest.raises(ValueError, match="no reading after 2025-10-08 22:00:00"):
            hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    else:
        staged, _, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
        assert staged["Reservoir_end_target"] == {NAME: {"mode": "free", "MWh": None}}
        assert assumptions[2].startswith("end target: none")


def test_unknown_end_mode_raises(settings_for):
    settings = settings_for(historical_hydro_end="equals")
    with pytest.raises(ValueError, match="historical_hydro_end must be one of"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


def test_weekly_path_targets_lie_at_the_readings_inside_the_period(settings_for):
    period = "2025-10-01/2025-10-19"  # readings inside: Monday 6 and 13 October; the end is the reading of 20 October
    settings = settings_for(period, historical_hydro_path="bfe_weekly")
    staged, _, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    time_map = hi.historical_time_map(period)
    step_ending = dict(zip(time_map["time_utc"] + pd.Timedelta(hours=1), time_map["t"]))
    fill, total = readings(settings), sum(OPEN_STORAGE.values())
    expected = {(step_ending[pd.Timestamp(instant, tz="UTC")], "TotalCH", NAME):
                pytest.approx(fill[date] * total, rel=TOLERANCE)
                for date, instant in (("2025-10-06", "2025-10-05 22:00"), ("2025-10-13", "2025-10-12 22:00"))}
    assert staged["Reservoir_path_target"] == expected
    assert list(staged["Reservoir_path_target"]) == list(expected)  # in time order
    assert assumptions[3].startswith("weekly path: stock of each reservoir group equal to 2 BFE readings")


def test_no_path_by_default(settings_for):
    settings = settings_for("2025-10-01/2025-10-19")
    staged, _, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    assert staged["Reservoir_path_target"] == {} and len(assumptions) == 3


def test_weekly_path_without_a_reading_inside_the_period_raises(settings_for):
    settings = settings_for(historical_hydro_path="bfe_weekly")  # PERIOD ends at its first reading after the start
    with pytest.raises(ValueError, match="no BFE reading lies inside"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


@pytest.mark.parametrize("mode", ["weekly", 0, -48, 48.5, True, "48"])
def test_unknown_path_mode_raises(settings_for, mode):
    settings = settings_for(historical_hydro_path=mode)
    with pytest.raises(ValueError, match="historical_hydro_path must be False, 'bfe_weekly' or a positive whole"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


def test_path_every_48_hours_interpolates_between_the_readings(settings_for):
    period = "2025-10-01/2025-10-07"  # 2025-09-30 22:00 to 2025-10-07 22:00 UTC: targets after 48, 96 and 144 hours
    settings = settings_for(period, historical_hydro_path=48)
    staged, _, assumptions = hi.read_reservoir_stocks(NAME, settings, fleet(settings))
    time_map = hi.historical_time_map(period)
    step_ending = dict(zip(time_map["time_utc"] + pd.Timedelta(hours=1), time_map["t"]))
    fill, total = readings(settings), sum(OPEN_STORAGE.values())
    # readings at 2025-09-28 22:00, 2025-10-05 22:00 and 2025-10-12 22:00 UTC
    expected_fill = {"2025-10-02 22:00": fill["2025-09-29"] + (fill["2025-10-06"] - fill["2025-09-29"]) * 96 / 168,
                     "2025-10-04 22:00": fill["2025-09-29"] + (fill["2025-10-06"] - fill["2025-09-29"]) * 144 / 168,
                     "2025-10-06 22:00": fill["2025-10-06"] + (fill["2025-10-13"] - fill["2025-10-06"]) * 24 / 168}
    expected = {(step_ending[pd.Timestamp(instant, tz="UTC")], "TotalCH", NAME): pytest.approx(f * total, rel=TOLERANCE)
                for instant, f in expected_fill.items()}
    assert staged["Reservoir_path_target"] == expected
    assert list(staged["Reservoir_path_target"]) == list(expected)
    assert assumptions[3].startswith("path every 48 hours: stock of each reservoir group at 3 instants")


def test_path_every_n_hours_leaves_the_end_to_the_end_target(settings_for):
    period = "2025-10-01/2025-10-04"  # 96 hours: the instant after 96 hours is the end
    settings = settings_for(period, historical_hydro_path=48)
    staged = hi.read_reservoir_stocks(NAME, settings, fleet(settings))[0]
    time_map = hi.historical_time_map(period)
    assert list(staged["Reservoir_path_target"]) == [(time_map["t"].iloc[47], "TotalCH", NAME)]


def test_path_every_n_hours_without_an_instant_inside_the_period_raises(settings_for):
    settings = settings_for("2025-10-01/2025-10-01", historical_hydro_path=48)  # a 24-hour period
    with pytest.raises(ValueError, match="no target instant lies inside"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


REGIONS = ("Wallis", "Graubuenden", "Tessin", "UebrigCH")
WASTA_REGIONS = {"historical_hydro_fleet": "wasta_individual", "historical_hydro_regions": "bfe_regions"}


def test_regions_group_the_open_wasta_plants_by_canton(settings_for):
    settings = settings_for(**WASTA_REGIONS)
    groups = hi.read_reservoir_stocks(NAME, settings, fleet(settings))[0]["Reservoir_group"]
    plants = pd.read_csv(hi.WASTA_PLANTS, encoding="utf-8-sig")
    open_plants = plants[plants["category"].isin(["dam", "psp_open"])]
    by_canton = {"VS": "Wallis", "GR": "Graubuenden", "TI": "Tessin"}
    assert groups == {hi.wasta_plant(c, n): by_canton.get(k, "UebrigCH")
                      for c, n, k in zip(open_plants["category"], open_plants["name"], open_plants["canton"])}
    assert groups["dam_Bieudron"] == "Wallis" and set(groups.values()) == set(REGIONS)


def test_regional_stocks_follow_each_regions_own_fill(settings_for):
    period = "2025-10-01/2025-10-19"  # readings inside: 6 and 13 October; the end is the reading of 20 October
    settings = settings_for(period, historical_hydro_path="bfe_weekly", **WASTA_REGIONS)
    plants = fleet(settings)
    staged, _, assumptions = hi.read_reservoir_stocks(NAME, settings, plants)
    table = pd.read_csv(Path(settings["historical_data_dir"]) / hi.RESERVOIR_TABLE).set_index("date")
    fill = {r: table[f"{r}_GWh"] / table[f"{r}_max_GWh"] for r in REGIONS}
    groups = staged["Reservoir_group"]
    storage = {p: plants["Plant_capacity_strg"][(p, NAME)] for p in groups}
    for p, r in groups.items():  # 48 hours after the reading of 29 September, in the plant's own region
        start = fill[r]["2025-09-29"] + (fill[r]["2025-10-06"] - fill[r]["2025-09-29"]) * 48 / 168
        assert staged["Reservoir_start_stock"][(p, NAME)] == pytest.approx(start * storage[p], rel=TOLERANCE)
    total = {r: sum(storage[p] for p in groups if groups[p] == r) for r in REGIONS}
    assert staged["Reservoir_end_target"][NAME]["MWh"] == {
        r: pytest.approx(fill[r]["2025-10-20"] * total[r], rel=TOLERANCE) for r in REGIONS}
    time_map = hi.historical_time_map(period)
    step_ending = dict(zip(time_map["time_utc"] + pd.Timedelta(hours=1), time_map["t"]))
    expected = {(step_ending[pd.Timestamp(instant, tz="UTC")], r, NAME): pytest.approx(fill[r][date] * total[r],
                                                                                       rel=TOLERANCE)
                for date, instant in (("2025-10-06", "2025-10-05 22:00"), ("2025-10-13", "2025-10-12 22:00"))
                for r in REGIONS}
    assert staged["Reservoir_path_target"] == expected
    assert list(staged["Reservoir_path_target"]) == list(expected)  # in time order, then by region
    assert assumptions[4].startswith("reservoir groups: the BFE regions, by the canton of each power station")


def test_regions_need_the_wasta_fleet(settings_for):
    settings = settings_for(historical_hydro_regions="bfe_regions")
    with pytest.raises(ValueError, match="historical_hydro_regions = 'bfe_regions' needs historical_hydro_fleet = "
                                         "'wasta_individual'"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


@pytest.mark.parametrize("mode", ["cantons", True, "bfe_region"])
def test_unknown_regions_mode_raises(settings_for, mode):
    settings = settings_for(historical_hydro_regions=mode)
    with pytest.raises(ValueError, match="historical_hydro_regions must be False or 'bfe_regions'"):
        hi.read_reservoir_stocks(NAME, settings, fleet(settings))


def test_every_input_is_built():
    assert hi.NOT_YET_BUILT == []


# --- model side ---------------------------------------------------------------------------------------------------

SF = 1e-3  # a non-unit scale of the storage balance
STORAGE = {"big": 1000.0, "small": 100.0, "loop": 50.0}  # two open plants of unequal capacity and a closed loop


@pytest.fixture
def boundaries(monkeypatch):
    def set_targets(mode="equal", target=620.0):
        monkeypatch.setitem(ch.Reservoir_start_stock, ("big", "s"), 400.0)
        monkeypatch.setitem(ch.Reservoir_start_stock, ("small", "s"), 40.0)
        monkeypatch.setitem(ch.Reservoir_group, "big", "TotalCH")
        monkeypatch.setitem(ch.Reservoir_group, "small", "TotalCH")
        monkeypatch.setitem(ch.Reservoir_end_target, "s",
                            {"mode": mode, "MWh": None if mode == "free" else {"TotalCH": target}})
    return set_targets


def storage_model():
    """Three storage plants over three cyclic steps with the production balance and start condition."""
    m = pyo.ConcreteModel()
    m.P = pyo.Set(initialize=list(STORAGE))
    m.P_storage = pyo.Set(initialize=list(STORAGE))
    m.P_pumping = pyo.Set(initialize=["big", "loop"])
    m.P_gen = pyo.Set(initialize=list(STORAGE))
    m.P_hydro = pyo.Set(initialize=list(STORAGE))
    m.P_inflow = pyo.Set(initialize=["big", "small"])
    m.P_outflow = pyo.Set(initialize=[])
    m.P_ev = pyo.Set(initialize=[])
    m.T = pyo.Set(initialize=["t_1", "t_2", "t_3"], ordered=True)
    m.Scenarios = pyo.Set(initialize=["s"])
    m.ConstraintNames = pyo.Set(initialize=["storage_soc", "storage_start_condition"])
    m.constraint_scaling = pyo.Param(m.ConstraintNames, initialize=SF)
    # electrical basis for the open plants, workbook values for the closed loop
    m.storage_charge_eff_in = pyo.Param(m.P_storage, m.Scenarios,
                                        initialize={("big", "s"): 0.7569, ("small", "s"): 0.7569, ("loop", "s"): 0.87})
    m.storage_charge_eff_out = pyo.Param(m.P_storage, m.Scenarios,
                                         initialize={("big", "s"): 1.0, ("small", "s"): 1.0, ("loop", "s"): 0.87})
    m.inflow = pyo.Param(m.P_inflow, m.T, m.Scenarios, initialize=30.0)
    m.storage_start_cond = pyo.Param(m.P_storage, m.Scenarios, initialize=0.95)
    m.gen_energy_max = pyo.Var(m.P, m.Scenarios)
    for p, capacity in STORAGE.items():
        m.gen_energy_max[p, "s"].fix(capacity)
    for name in ("soc", "storage_charge", "gen", "spill_water"):
        m.add_component(name, pyo.Var(m.P, m.T, m.Scenarios, initialize=0.0))
    m.storage_soc = pyo.Constraint(m.P_storage, m.T, m.Scenarios, rule=cc.storeBalance_Constraint)
    m.storage_start_condition = pyo.Constraint(m.P_storage, m.Scenarios, rule=cc.storage_start_condition_Constraint)
    return m


def holds(row):
    body = pyo.value(row.body)
    return bool((row.lower is None or body >= pyo.value(row.lower) - TOLERANCE)
                and (row.upper is None or body <= pyo.value(row.upper) + TOLERANCE))


@pytest.mark.parametrize("mode, final, accepted", [
    ("equal", (600.0, 20.0), True),  # fill fractions 0.6 and 0.2, sum 620 = target
    ("equal", (600.0, 21.0), False),
    ("equal", (650.0, 20.0), False),
    ("at_least", (600.0, 20.0), True),
    ("at_least", (650.0, 20.0), True),
    ("at_least", (590.0, 20.0), False),
])
def test_national_end_target(boundaries, mode, final, accepted):
    boundaries(mode)
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    m.soc["big", "t_3", "s"].value, m.soc["small", "t_3", "s"].value = final
    assert holds(m.reservoir_end_target["TotalCH", "s"]) is accepted
    assert ch.registered_constraints(m) == ["reservoir_first_hour", "reservoir_end_target"]


def test_free_end_adds_no_target(boundaries):
    boundaries("free")
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    assert m.component("reservoir_end_target") is None and ch.registered_constraints(m) == ["reservoir_first_hour"]


def coefficients(row):
    repn = generate_standard_repn(row.body, compute_values=True)
    return {var.name: coef for var, coef in zip(repn.linear_vars, repn.linear_coefs)}, repn.constant


def test_first_hour_has_the_production_coefficients_with_the_start_stock_before_the_hour(boundaries):
    boundaries()
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    production, production_constant = coefficients(m.storage_soc["big", "t_1", "s"])  # deactivated, still built
    first, first_constant = coefficients(m.reservoir_first_hour["big", "s"])
    previous = m.soc["big", "t_3", "s"].name  # the cyclic link of the first hour
    assert set(first) == set(production) - {previous}
    assert {"storage_charge[big,t_1,s]", "gen[big,t_1,s]", "spill_water[big,t_1,s]"} <= set(first)
    for var, coef in first.items():  # unscaled: the production row divided by its scale
        assert coef * SF == pytest.approx(production[var], rel=TOLERANCE)
    assert first["soc[big,t_1,s]"] == 1.0
    # inflow (30) on the right side; the start stock replaces the previous stock
    assert first_constant == pytest.approx(-(400.0 + 30.0)) and production_constant == pytest.approx(-30.0 * SF)


def test_only_the_first_rows_of_the_open_plants_change(boundaries):
    boundaries()
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    for p in ("big", "small"):
        assert not m.storage_soc[p, "t_1", "s"].active and not m.storage_start_condition[p, "s"].active
        assert m.storage_soc[p, "t_2", "s"].active and m.storage_soc[p, "t_3", "s"].active
    assert all(m.storage_soc["loop", t, "s"].active for t in m.T) and m.storage_start_condition["loop", "s"].active


def test_first_hour_and_end_target_duals_are_exported_in_EUR_per_MWh(boundaries, tmp_path, monkeypatch):
    boundaries()
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    for row in m.storage_soc.values():
        if row.active:
            m.dual[row] = 2.0 / SF  # the solver dual of a scaled row
    m.dual[m.reservoir_first_hour["big", "s"]] = -55.0
    m.dual[m.reservoir_first_hour["small", "s"]] = -54.0
    m.dual[m.reservoir_end_target["TotalCH", "s"]] = 61.5
    monkeypatch.chdir(tmp_path)
    res_export.constraints([m.storage_soc, m.reservoir_first_hour, m.reservoir_end_target], "run", m)
    folder = tmp_path / "output" / "run"
    assert pd.read_csv(folder / "reservoir_first_hour_dual.csv")["value"].tolist() == [-55.0, -54.0]
    assert pd.read_csv(folder / "reservoir_end_target_dual.csv")["value"].tolist() == [61.5]
    storage = pd.read_csv(folder / "storage_soc_dual.csv")
    assert storage["value"].tolist() == pytest.approx([2.0] * (9 - 2))  # the first rows of big and small are left out


@pytest.mark.parametrize("change, message", [
    (lambda mp: mp.setattr(ch, "Reservoir_start_stock", {}), "no start stock is staged"),
    (lambda mp: mp.setitem(ch.Reservoir_start_stock, ("loop", "s"), 10.0), "loop is no storage plant with inflow"),
    (lambda mp: mp.setitem(ch.Reservoir_end_target, "s", {"mode": "equals", "MWh": {"TotalCH": 1.0}}),
     "no valid end target for s"),
    (lambda mp: mp.delitem(ch.Reservoir_group, "small"), "small has no reservoir group"),
    (lambda mp: mp.setitem(ch.Reservoir_end_target, "s", {"mode": "equal", "MWh": {"other": 1.0}}),
     "no end target of group TotalCH in s"),
])
def test_missing_or_invalid_boundaries_raise(boundaries, monkeypatch, change, message):
    boundaries()
    change(monkeypatch)
    with pytest.raises(ValueError, match=message):
        ch.add_reservoir_boundaries(storage_model())


@pytest.mark.parametrize("big, small, loop, accepted", [
    (450.0, 50.0, 0.0, True),
    (450.0, 50.0, 30.0, True),  # the closed loop is in no reservoir group
    (451.0, 50.0, 0.0, False),
    (450.0, 49.0, 0.0, False),
])
def test_weekly_path_target_binds_the_national_stock_after_its_step(boundaries, monkeypatch, big, small, loop, accepted):
    boundaries()
    monkeypatch.setitem(ch.Reservoir_path_target, ("t_2", "TotalCH", "s"), 500.0)
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    m.soc["big", "t_2", "s"].value, m.soc["small", "t_2", "s"].value, m.soc["loop", "t_2", "s"].value = big, small, loop
    assert holds(m.reservoir_path_target["t_2", "TotalCH", "s"]) is accepted
    assert list(m.reservoir_path_target) == [("t_2", "TotalCH", "s")]
    assert ch.registered_constraints(m) == ["reservoir_first_hour", "reservoir_end_target", "reservoir_path_target"]


def test_weekly_path_duals_are_exported_by_step_in_EUR_per_MWh(boundaries, monkeypatch, tmp_path):
    boundaries()
    monkeypatch.setitem(ch.Reservoir_path_target, ("t_1", "TotalCH", "s"), 450.0)
    monkeypatch.setitem(ch.Reservoir_path_target, ("t_2", "TotalCH", "s"), 500.0)
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    m.dual = pyo.Suffix(direction=pyo.Suffix.IMPORT)
    m.dual[m.reservoir_path_target["t_1", "TotalCH", "s"]] = 12.5
    m.dual[m.reservoir_path_target["t_2", "TotalCH", "s"]] = -3.0
    monkeypatch.chdir(tmp_path)
    res_export.constraints([m.reservoir_path_target], "run", m)
    duals = pd.read_csv(tmp_path / "output" / "run" / "reservoir_path_target_dual.csv")
    assert duals.to_dict("list") == {"T_reservoir_path": ["t_1", "t_2"], "Reservoir_groups": ["TotalCH", "TotalCH"],
                                     "Scenarios": ["s", "s"], "value": [12.5, -3.0]}


def test_weekly_path_target_at_an_unknown_step_raises(boundaries, monkeypatch):
    boundaries()
    monkeypatch.setitem(ch.Reservoir_path_target, ("t_9", "TotalCH", "s"), 500.0)
    with pytest.raises(ValueError, match="path target step t_9 is no model step"):
        ch.add_reservoir_boundaries(storage_model())


@pytest.mark.parametrize("big, small, holds_a, holds_b", [
    (600.0, 20.0, True, True),
    (600.0, 21.0, True, False),
    (599.0, 20.0, False, True),
])
def test_each_group_meets_its_own_targets(boundaries, monkeypatch, big, small, holds_a, holds_b):
    boundaries()
    monkeypatch.setitem(ch.Reservoir_group, "big", "A")
    monkeypatch.setitem(ch.Reservoir_group, "small", "B")
    monkeypatch.setitem(ch.Reservoir_end_target, "s", {"mode": "equal", "MWh": {"A": 600.0, "B": 20.0}})
    monkeypatch.setitem(ch.Reservoir_path_target, ("t_2", "A", "s"), 600.0)
    monkeypatch.setitem(ch.Reservoir_path_target, ("t_2", "B", "s"), 20.0)
    m = storage_model()
    ch.add_reservoir_boundaries(m)
    for t in ("t_2", "t_3"):
        m.soc["big", t, "s"].value, m.soc["small", t, "s"].value = big, small
    assert holds(m.reservoir_end_target["A", "s"]) is holds_a and holds(m.reservoir_end_target["B", "s"]) is holds_b
    assert holds(m.reservoir_path_target["t_2", "A", "s"]) is holds_a
    assert holds(m.reservoir_path_target["t_2", "B", "s"]) is holds_b


def test_hook_adds_the_reservoir_boundaries(boundaries, monkeypatch):
    boundaries()
    monkeypatch.setattr(ch, "check_historical_model", lambda model, scenario_name, preset_file: None)
    monkeypatch.setattr(ch, "add_fixed_generation", lambda model: None)
    monkeypatch.setattr(ch, "add_hydro_operation", lambda model: None)  # tested on its own
    m = storage_model()
    ch.apply_historical_constraints(m, "hist", "unused.csv")
    assert ch.registered_constraints(m) == ["reservoir_first_hour", "reservoir_end_target"]
