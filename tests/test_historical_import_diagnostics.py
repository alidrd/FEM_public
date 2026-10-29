"""Unit tests for the two back-cast diagnostics in data_prep/historical_import.py and model/components_historical.py:
border flows fixed to the observed scheduled exchange (historical_border_flows) and plant groups fixed to their
ENTSO-E A73 output (historical_hydro_fixed_units). No model is solved."""
import pandas as pd
import pyomo.environ as pyo
import pytest
from pyomo.repn import generate_standard_repn

import model.read_settings as read_settings
from data_prep import historical_import as hi
from model import components_historical as ch
from tests.backcast_data import INTO_CH, OUT_OF_CH, period_hours, write_processed

NAME = "hist_diagnostics_obs"
PERIOD = "2025-10-01/2025-10-02"  # 48 hours, model steps t_6553 to t_6600; row k + 1 of a table is hour k
GROUP_COLUMNS = ["group", "entsoe_units", "plants", "fix", "note"]


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(**changes):
        folder = write_processed(tmp_path / "processed", PERIOD)
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": PERIOD,
            "historical_data_dir": str(folder), **changes})
        return read_settings.read_scenario_settings(NAME)
    return make


# --- border flows ---------------------------------------------------------------------------------------------------

def write_exchanges(settings, values):
    """Scheduled exchange table (+ import) with values(neighbour, k) in hour k, plus one hour on each side."""
    hours = period_hours(PERIOD)
    table = pd.DataFrame({"time_utc": hours, **{n: [values(n, k - 1) for k in range(len(hours))] for n in INTO_CH}})
    table.to_csv(f"{settings['historical_data_dir']}/{hi.EXCHANGE_TABLE}", index=False)


def test_scheduled_mode_fixes_each_line_to_the_observed_exchange(settings_for):
    settings = settings_for(historical_border_flows="scheduled")
    write_exchanges(settings, lambda n, k: (100.0 + k) if n in ("DE00", "FR00") else -(50.0 + k))
    staged, files, assumptions = hi.read_market(NAME, settings)
    for k, t in enumerate(settings["T_list"]):
        for n in INTO_CH:
            observed = (100.0 + k) if n in ("DE00", "FR00") else -(50.0 + k)
            line = (f"HVAC_{n}_CH00", t, NAME)
            # ATCbound_Constraint: -ATC_importlimit <= Export <= ATC_exportlimit, so both bounds are the observed flow
            assert staged["ATC_exportlimit"][line] == observed and -staged["ATC_importlimit"][line] == observed
    assert files[-1].endswith(hi.EXCHANGE_TABLE)
    assert "border flows fixed to the observed scheduled exchange" in assumptions[1]


def test_scheduled_mode_counts_hours_outside_the_transfer_limits(settings_for):
    settings = settings_for(historical_border_flows="scheduled", NTC_CH_ratio=1.0)
    # AT: import above its 1200 MW limit in hours 0-2; IT: export above its 3716 MW limit in hour 5
    write_exchanges(settings, lambda n, k: {"AT00": 1300.0 if k < 3 else 0.0,
                                            "IT00": -3800.0 if k == 5 else 0.0}.get(n, 0.0))
    assumptions = hi.read_market(NAME, settings)[2]
    assert "{'AT00': 3, 'DE00': 0, 'FR00': 0, 'IT00': 1}" in assumptions[1]
    assert INTO_CH["AT00"] == 1200.0 and OUT_OF_CH["IT00"] == 3716.0


def test_ntc_mode_keeps_the_transfer_limits_and_an_unknown_mode_raises(settings_for):
    staged = hi.read_market(NAME, settings_for())[0]
    first = settings_for()["T_list"][0]
    assert staged["ATC_exportlimit"][("HVAC_AT00_CH00", first, NAME)] == INTO_CH["AT00"]
    with pytest.raises(ValueError, match=r"historical_border_flows must be one of \['ntc', 'scheduled'\]"):
        hi.read_market(NAME, settings_for(historical_border_flows="observed"))


def test_scheduled_mode_without_an_exchange_value_raises(settings_for):
    settings = settings_for(historical_border_flows="scheduled")
    write_exchanges(settings, lambda n, k: float("nan") if (n, k) == ("FR00", 7) else 0.0)
    with pytest.raises(ValueError, match=f"{hi.EXCHANGE_TABLE}: 1 missing or non-finite values of FR00"):
        hi.read_market(NAME, settings)


# --- fixed plant groups: data side ------------------------------------------------------------------------------------

def write_units(settings, values):
    """Unit table with values[unit](k) in hour k, plus one hour on each side."""
    hours = period_hours(PERIOD)
    table = pd.DataFrame({"time_utc": hours, **{u: [f(k - 1) for k in range(len(hours))] for u, f in values.items()}})
    table.to_csv(f"{settings['historical_data_dir']}/{hi.UNITS_TABLE}", index=False)


def group_file(tmp_path, rows):
    path = tmp_path / "groups.csv"
    pd.DataFrame(rows, columns=GROUP_COLUMNS).to_csv(path, index=False)
    return str(path)


ROWS = [("A", "U1;U2", "dam_Bieudron", "yes", ""), ("B", "U3", "dam_Verbano_1;dam_Verbano_2", "no", "not fixed")]


def read_units(settings):
    fleet = hi.read_hydro_fleet(NAME, settings)[0]
    inflow = hi.read_inflow(NAME, settings, fleet["Plant_list"], hi.pump_round_trip(fleet, NAME))[0]
    stocks = hi.read_reservoir_stocks(NAME, settings, fleet)[0]
    return hi.read_fixed_units(NAME, settings, fleet, inflow, stocks)


@pytest.fixture
def unit_settings(settings_for, tmp_path):
    def make(rows=ROWS, units=None, **changes):
        settings = settings_for(historical_hydro_fleet="wasta_individual",
                                historical_hydro_fixed_units=group_file(tmp_path, rows), **changes)
        write_units(settings, units or {"U1": lambda k: 100.0 + k, "U2": lambda k: 50.0, "U3": lambda k: 7.0})
        return settings
    return make


def test_a_group_produces_the_sum_of_its_units_in_every_hour(unit_settings):
    settings = unit_settings()
    staged, files, assumptions = read_units(settings)
    assert staged["Hydro_fixed_group"] == {"A": ["dam_Bieudron"]}  # B has fix = no
    assert staged["Hydro_fixed_output"] == {("A", t, NAME): 150.0 + k for k, t in enumerate(settings["T_list"])}
    assert files[0] == settings["historical_hydro_fixed_units"] and files[1].endswith(hi.UNITS_TABLE)
    assert "A (dam_Bieudron; units U1, U2): 48 of 48 hours published" in assumptions[0]


def test_a_group_is_free_where_one_of_its_units_is_unpublished(unit_settings):
    settings = unit_settings(units={"U1": lambda k: 100.0, "U2": lambda k: float("nan") if k == 5 else 50.0,
                                    "U3": lambda k: 7.0})
    staged, _, assumptions = read_units(settings)
    unpublished = settings["T_list"][5]
    assert ("A", unpublished, NAME) not in staged["Hydro_fixed_output"] and len(staged["Hydro_fixed_output"]) == 47
    assert "47 of 48 hours published" in assumptions[0]


def test_output_above_available_capacity_by_rounding_is_cut_and_by_more_raises(unit_settings):
    settings = unit_settings()
    fleet = hi.read_hydro_fleet(NAME, settings)[0]
    available = 1269.0 * fleet["Avail_plant"][("dam_Bieudron", settings["T_list"][3], NAME)]  # workbook availability
    settings = unit_settings(units={"U1": lambda k: available + 0.5 if k == 3 else 100.0, "U2": lambda k: 0.0,
                                    "U3": lambda k: 7.0})
    staged, _, assumptions = read_units(settings)
    assert staged["Hydro_fixed_output"][("A", settings["T_list"][3], NAME)] == pytest.approx(available)
    assert "cut to the capacity, hours per group: {'A': 1}" in assumptions[1]
    settings = unit_settings(units={"U1": lambda k: available + 1.5 if k == 3 else 100.0, "U2": lambda k: 0.0,
                                    "U3": lambda k: 7.0})
    with pytest.raises(ValueError, match=rf"group A: observed output {available + 1.5:.1f} MW above its available "
                                         rf"capacity {available:.1f} MW"):
        read_units(settings)


def test_a_dam_group_needing_more_water_than_it_holds_raises_and_a_pumped_group_is_not_checked(unit_settings):
    bitsch = [("A", "U1", "dam_Bitsch", "yes", "")]  # 14 GWh of storage, about 9 GWh at the start
    with pytest.raises(ValueError, match=r"group A: its observed output needs [\d,]+ MWh more water"):
        read_units(unit_settings(rows=bitsch, units={"U1": lambda k: 320.0}))  # below its available 328 MW
    nant = [("A", "U1", "psp_close_Nant_de_Drance", "yes", "")]  # pumps can refill it: no check
    staged = read_units(unit_settings(rows=nant, units={"U1": lambda k: 800.0}))[0]
    assert len(staged["Hydro_fixed_output"]) == 48


def test_water_spilled_from_a_full_reservoir_cannot_supply_later_output():
    # storage 100 MWh, full at the start; 50 MWh of inflow in t_1 spill; 120 MWh in t_2 then exceed the 100 left
    fleet = {"Plant_capacity_strg": {("d", "s"): 100.0}, "Map_eff_out_plant": {"d": 1.0}}
    inflow = {"Inflow_data": {("d", "t_1", "s"): 50.0, ("d", "t_2", "s"): 0.0}}
    stocks = {"Reservoir_start_stock": {("d", "s"): 100.0}}
    output = pd.Series({"t_1": 0.0, "t_2": 120.0})
    with pytest.raises(ValueError, match="group g: its observed output needs 20 MWh more water than its start stock "
                                         "and inflow hold by t_2"):
        hi.check_fixed_water("g", ["d"], output, fleet, inflow, stocks, "s")
    hi.check_fixed_water("g", ["d"], output.replace(120.0, 100.0), fleet, inflow, stocks, "s")


@pytest.mark.parametrize("rows, units, message", [
    ([("A", "U1", "dam_Bieudron", "maybe", "")], None, "fix must be yes or no, got 'maybe'"),
    ([("A", "U1", "dam_Nowhere", "yes", "")], None, "dam_Nowhere is no storage-hydro plant of the WASTA fleet"),
    ([("A", "U1", "dam_Bieudron", "yes", ""), ("B", "U2", "dam_Bieudron", "no", "")], None,
     "dam_Bieudron is in the groups A and B"),
    ([("A", "U1", "dam_Bieudron", "yes", ""), ("B", "U1", "dam_Bitsch", "no", "")], None, "unit U1 is in the groups"),
    ([("A", "U1", "dam_Bieudron", "yes", ""), ("A", "U2", "dam_Bitsch", "yes", "")], None, "groups named twice"),
    ([("A", "", "dam_Bieudron", "yes", "")], None, "group A needs at least one plant and one unit"),
    ([("A", "U1", "dam_Bieudron", "no", "")], None, "no group with fix = yes"),
    ([("A", "U9", "dam_Bieudron", "yes", "")], None, r"no column \['U9'\]"),
    ([("A", "U1", "dam_Bieudron", "yes", "")], {"U1": lambda k: -1.0}, "non-finite or negative output"),
    ([("A", "U1", "dam_Bieudron", "yes", "")], {"U1": lambda k: float("nan")}, "no published hour of the units of group A"),
])
def test_invalid_groups_and_units_raise(unit_settings, rows, units, message):
    with pytest.raises(ValueError, match=message):
        read_units(unit_settings(rows=rows, units=units))


def test_fixed_units_need_the_wasta_fleet_and_an_existing_file(settings_for, tmp_path):
    with pytest.raises(ValueError, match="needs historical_hydro_fleet = 'wasta_individual'"):
        read_units(settings_for(historical_hydro_fixed_units=group_file(tmp_path, ROWS)))
    with pytest.raises(ValueError, match="historical_hydro_fixed_units: no such file"):
        read_units(settings_for(historical_hydro_fleet="wasta_individual",
                                historical_hydro_fixed_units=str(tmp_path / "missing.csv")))


def test_everything_is_off_by_default(settings_for):
    assert read_units(settings_for()) == ({"Hydro_fixed_group": {}, "Hydro_fixed_output": {}}, [], [])


def test_the_turbine_caps_of_fixed_plants_are_lifted_and_their_pump_caps_kept(settings_for, tmp_path):
    outages = tmp_path / "outages.csv"
    pd.DataFrame([("dam_Bieudron", "2025-10-01 00:00", "2025-10-01 03:00", 400, 0, "test"),
                  ("psp_close_Veytaux", "2025-10-01 00:00", "2025-10-01 02:00", 300, 200, "test")],
                 columns=["plant", "start_local", "end_local", "turbine_MW", "pump_MW", "source"]).to_csv(outages,
                                                                                                         index=False)
    settings = settings_for(historical_hydro_fleet="wasta_individual", historical_hydro_outages=str(outages))
    fleet = hi.read_hydro_fleet(NAME, settings)[0]
    caps, _, assumptions = hi.read_hydro_operation(NAME, settings, fleet, ["dam_Bieudron", "psp_close_Veytaux"])
    first = settings["T_list"][0]
    assert caps["Hydro_outage_cap"][("dam_Bieudron", first, NAME)] == (1269.0, 0.0)
    assert caps["Hydro_outage_cap"][("psp_close_Veytaux", first, NAME)] == (420.0, 200.0)
    assert "turbine caps of the fixed plants" in assumptions[-1] and "'dam_Bieudron': 3" in assumptions[-1]
    unfixed = hi.read_hydro_operation(NAME, settings, fleet)[0]["Hydro_outage_cap"]
    assert unfixed[("dam_Bieudron", first, NAME)] == (400.0, 0.0)


# --- fixed plant groups: model side -------------------------------------------------------------------------------

S = "s"
TECHS = {"dam_a": "dam", "open_b": "psp_open", "nuke": "nuclear"}


@pytest.fixture
def fixed(monkeypatch):
    def stage(groups, outputs):
        for p, tech in TECHS.items():
            monkeypatch.setitem(ch.Map_plant_tech, p, tech)
        for g, plants in groups.items():
            monkeypatch.setitem(ch.Hydro_fixed_group, g, plants)
        for key, value in outputs.items():
            monkeypatch.setitem(ch.Hydro_fixed_output, key, value)
    return stage


def small_model():
    m = pyo.ConcreteModel()
    m.P_gen = pyo.Set(initialize=list(TECHS))
    m.T = pyo.Set(initialize=["t_1", "t_2", "t_3"], ordered=True)
    m.Scenarios = pyo.Set(initialize=[S])
    m.gen = pyo.Var(m.P_gen, m.T, m.Scenarios, initialize=0.0)
    return m


def test_a_group_sum_equals_its_output_in_the_published_hours_only(fixed):
    fixed({"g": ["dam_a", "open_b"]}, {("g", "t_1", S): 70.0, ("g", "t_3", S): 20.0})
    m = small_model()
    ch.add_fixed_hydro_output(m)
    assert list(m.hydro_fixed_output) == [("g", "t_1", S), ("g", "t_3", S)]
    row = m.hydro_fixed_output["g", "t_1", S]
    repn = generate_standard_repn(row.body, compute_values=True)
    assert {v.name: c for v, c in zip(repn.linear_vars, repn.linear_coefs)} == {"gen[dam_a,t_1,s]": 1.0,
                                                                               "gen[open_b,t_1,s]": 1.0}
    assert pyo.value(row.lower) == pyo.value(row.upper) == 70.0
    assert ch.registered_constraints(m) == ["hydro_fixed_output"]


@pytest.mark.parametrize("groups, outputs, message", [
    ({"g": ["nuke"]}, {("g", "t_1", S): 1.0}, "group g: nuke is no storage-hydro plant of the model"),
    ({"g": ["dam_a"]}, {("h", "t_1", S): 1.0}, "fixed output of h for unknown group"),
    ({"g": ["dam_a"]}, {("g", "t_9", S): 1.0}, "fixed output step t_9 is no model step"),
])
def test_invalid_fixed_output_raises(fixed, groups, outputs, message):
    fixed(groups, outputs)
    with pytest.raises(ValueError, match=message):
        ch.add_fixed_hydro_output(small_model())


def test_nothing_is_added_without_fixed_output_and_the_hook_adds_it(fixed, monkeypatch):
    m = small_model()
    ch.add_fixed_hydro_output(m)
    assert m.component("hydro_fixed_output") is None and ch.registered_constraints(m) == []
    fixed({"g": ["dam_a"]}, {("g", "t_2", S): 5.0})
    for step in ("check_historical_model", "add_fixed_generation", "add_reservoir_boundaries", "add_hydro_operation"):
        monkeypatch.setattr(ch, step, lambda *args: None)
    m = small_model()
    ch.apply_historical_constraints(m, "hist", "unused.csv")
    assert ch.registered_constraints(m) == ["hydro_fixed_output"]
