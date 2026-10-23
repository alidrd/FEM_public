"""Unit tests for the gauge-based split of the national inflow between plants (historical_inflow_split =
'foen_gauges'): gauge_weekly_split and its use in read_inflow, data_prep/historical_import.py."""
import pandas as pd
import pytest

import model.read_settings as read_settings
from data_prep import historical_import as hi
from tests.backcast_data import write_processed

NAME = "hist_gauge_obs"
TOLERANCE = 1e-9
WEEKS = [pd.Timestamp("2025-09-29"), pd.Timestamp("2025-10-06")]


def discharge(station, values_per_week, days=7):
    rows = []
    for week, value in zip(WEEKS, values_per_week):
        rows += [(station, week + pd.Timedelta(days=d), value) for d in range(days)]
    return rows


def daily_table(*rows):
    return pd.DataFrame([r for part in rows for r in part], columns=["station", "date", "discharge_m3s"])


def test_split_follows_each_donors_timing_and_sums_to_one():
    shares = {"dam_A": 0.6, "dam_B": 0.4}
    daily = daily_table(discharge(1, [10.0, 10.0]), discharge(2, [1.0, 3.0]))  # donor 2: 0.5 and 1.5 of its mean
    split = hi.gauge_weekly_split(shares, {"dam_A": 1, "dam_B": 2}, daily, WEEKS)
    assert split.loc["dam_A", WEEKS[0]] == pytest.approx(0.6 / (0.6 + 0.4 * 0.5), rel=TOLERANCE)
    assert split.loc["dam_B", WEEKS[0]] == pytest.approx(0.2 / 0.8, rel=TOLERANCE)
    assert split.loc["dam_A", WEEKS[1]] == pytest.approx(0.5, rel=TOLERANCE)
    assert split.sum().tolist() == pytest.approx([1.0, 1.0], rel=TOLERANCE)


def test_split_uses_the_mean_of_the_days_present():
    shares = {"dam_A": 0.5, "dam_B": 0.5}
    five_days = [(2, WEEKS[0] + pd.Timedelta(days=d), 1.0) for d in range(5)]
    daily = daily_table(discharge(1, [2.0, 2.0]), five_days, [(2, WEEKS[1] + pd.Timedelta(days=d), 1.0) for d in range(7)])
    split = hi.gauge_weekly_split(shares, {"dam_A": 1, "dam_B": 2}, daily, WEEKS)
    assert split.loc["dam_B"].tolist() == pytest.approx([0.5, 0.5], rel=TOLERANCE)  # 5 days of 1.0 is a mean of 1.0


def test_a_week_with_too_few_days_raises():
    daily = daily_table(discharge(1, [2.0, 2.0]), discharge(2, [1.0, 1.0], days=4))
    with pytest.raises(ValueError, match="gauge 2 has 4 days of discharge in the week of 2025-09-29"):
        hi.gauge_weekly_split({"dam_A": 0.5, "dam_B": 0.5}, {"dam_A": 1, "dam_B": 2}, daily, WEEKS)


def test_a_plant_without_donor_raises():
    daily = daily_table(discharge(1, [2.0, 2.0]))
    with pytest.raises(ValueError, match=r"no donor gauge for the open plants \['dam_B'\]"):
        hi.gauge_weekly_split({"dam_A": 0.5, "dam_B": 0.5}, {"dam_A": 1}, daily, WEEKS)


@pytest.fixture
def settings_for(monkeypatch, tmp_path):
    def make(**changes):
        folder = write_processed(tmp_path / "processed", "2025-10-01/2025-10-07")
        monkeypatch.setitem(read_settings.scenarios_list, NAME, {
            "CH_only": True, "allow_investment": False, "NodeDH_list": [], "historical_period": "2025-10-01/2025-10-07",
            "historical_data_dir": str(folder), "historical_hydro_fleet": "wasta_individual", **changes})
        return read_settings.read_scenario_settings(NAME)
    return make


@pytest.fixture
def gauges(monkeypatch, tmp_path):
    """Two open plants with donors 1 (flat) and 2 (rising), in place of the WASTA fleet's shares and the FOEN files."""
    monkeypatch.setattr(hi, "fleet_inflow_shares", lambda settings: {"dam_A": 0.6, "dam_B": 0.4})
    donors = pd.DataFrame({"name": ["A", "B"], "category": ["dam", "dam"], "donor": [1, 2]})
    donors.to_csv(tmp_path / "plant_donors.csv", index=False)
    daily_table(discharge(1, [10.0, 10.0]), discharge(2, [1.0, 3.0])).to_csv(tmp_path / "daily.csv", index=False)
    monkeypatch.setattr(hi, "FOEN_DONORS", str(tmp_path / "plant_donors.csv"))
    monkeypatch.setattr(hi, "FOEN_DAILY", str(tmp_path / "daily.csv"))


def test_read_inflow_splits_each_hour_by_the_week_of_its_gauges(settings_for, gauges):
    plain = hi.read_inflow(NAME, settings_for(), ["dam_A", "dam_B"], 0.7569)[0]["Inflow_data"]
    staged, files, assumptions = hi.read_inflow(NAME, settings_for(historical_inflow_split="foen_gauges"),
                                                ["dam_A", "dam_B"], 0.7569)
    inflow = staged["Inflow_data"]
    time_map = hi.historical_time_map("2025-10-01/2025-10-07")
    for t, utc in zip(time_map["t"], time_map["time_utc"]):
        total = plain[("dam_A", t, NAME)] + plain[("dam_B", t, NAME)]
        assert inflow[("dam_A", t, NAME)] + inflow[("dam_B", t, NAME)] == pytest.approx(total, rel=TOLERANCE)
        share_a = 0.6 / (0.6 + 0.4 * 0.5) if utc < pd.Timestamp("2025-10-06", tz="UTC") else 0.5
        assert inflow[("dam_A", t, NAME)] == pytest.approx(share_a * total, rel=TOLERANCE)
    assert hi.FOEN_DAILY in files and hi.FOEN_DONORS in files
    assert any(a.startswith("inflow split between plants by FOEN donor gauges") for a in assumptions)


def test_gauge_split_needs_the_wasta_fleet(settings_for, gauges):
    settings = settings_for(historical_hydro_fleet="representative_2030", historical_inflow_split="foen_gauges")
    with pytest.raises(ValueError, match="historical_inflow_split = 'foen_gauges' needs historical_hydro_fleet = "
                                         "'wasta_individual'"):
        hi.read_inflow(NAME, settings, ["large_psp"], 0.7569)


def test_unknown_split_raises(settings_for, gauges):
    with pytest.raises(ValueError, match="historical_inflow_split must be 'fixed_shares' or 'foen_gauges'"):
        hi.read_inflow(NAME, settings_for(historical_inflow_split="gauges"), ["dam_A"], 0.7569)
