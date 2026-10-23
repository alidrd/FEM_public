"""Unit tests for the ENTSO-E prices and Swiss generation of data_prep/historical (entsoe.py, generation.py, the
generation check of checks.py) and for retrying dropped connections in http_get.py. No request is sent."""
import urllib.error

import numpy as np
import pandas as pd
import pytest

from data_prep.historical import checks, entsoe, generation, http_get
from data_prep.historical.timeutils import hourly_index

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


def _series(points, psr=None, sequence=None, consumption=False, curve="A03", start="2025-10-01T22:00Z",
            end="2025-10-02T02:00Z", resolution="PT60M", price=False):
    tag = "price.amount" if price else "quantity"
    pts = "".join(f"<Point><position>{p}</position><{tag}>{q}</{tag}></Point>" for p, q in points)
    domain = ("<outBiddingZone_Domain.mRID>10YCH-SWISSGRIDZ</outBiddingZone_Domain.mRID>" if consumption
              else "<inBiddingZone_Domain.mRID>10YCH-SWISSGRIDZ</inBiddingZone_Domain.mRID>")
    psr_el = f"<MktPSRType><psrType>{psr}</psrType></MktPSRType>" if psr else ""
    seq_el = (f"<classificationSequence_AttributeInstanceComponent.position>{sequence}"
              f"</classificationSequence_AttributeInstanceComponent.position>" if sequence else "")
    return (f"<TimeSeries><curveType>{curve}</curveType>{domain}{seq_el}{psr_el}<Period><timeInterval>"
            f"<start>{start}</start><end>{end}</end></timeInterval><resolution>{resolution}</resolution>{pts}"
            f"</Period></TimeSeries>")


def _document(*series):
    return f'<GL_MarketDocument xmlns="{NS}">{"".join(series)}</GL_MarketDocument>'


# --- parse_series -----------------------------------------------------------------------------------------------

def test_series_are_keyed_by_production_type_and_a03_blocks_hold():
    found = entsoe.parse_series(_document(_series([(1, 10), (3, 30)], psr="B16"),
                                          _series([(1, 5), (2, 6), (3, 7), (4, 8)], psr="B11")))
    assert set(found) == {("B16", None, "generation"), ("B11", None, "generation")}
    assert found[("B16", None, "generation")].tolist() == [10, 10, 30, 30]
    assert found[("B11", None, "generation")].index[0] == pd.Timestamp("2025-10-01 22:00", tz="UTC")


def test_prices_sequences_and_consumption_get_their_own_keys():
    found = entsoe.parse_series(_document(_series([(1, 50), (2, 60), (3, 70), (4, 80)], sequence="1", price=True),
                                          _series([(1, 1), (2, 2), (3, 3), (4, 4)], sequence="2", price=True),
                                          _series([(1, 9), (2, 9), (3, 9), (4, 9)], psr="B10", consumption=True)))
    assert found[(None, "1", "generation")].tolist() == [50, 60, 70, 80]
    assert found[(None, "2", "generation")].tolist() == [1, 2, 3, 4]
    assert ("B10", None, "consumption") in found


def test_series_split_over_several_periods_are_joined():
    first = _series([(1, 1), (2, 2)], psr="B14", end="2025-10-02T00:00Z")
    second = _series([(1, 3), (2, 4)], psr="B14", start="2025-10-02T00:00Z")
    series = entsoe.parse_series(_document(first, second))[("B14", None, "generation")]
    assert series.tolist() == [1, 2, 3, 4] and series.index.is_monotonic_increasing


def test_a_stamp_given_twice_unknown_curve_and_no_data_raise():
    with pytest.raises(ValueError, match="two values for"):
        entsoe.parse_series(_document(_series([(1, 1)], psr="B14", curve="A01"),
                                      _series([(1, 2)], psr="B14", curve="A01")))
    with pytest.raises(ValueError, match="unsupported curveType A02"):
        entsoe.parse_series(_document(_series([(1, 1)], psr="B14", curve="A02")))
    with pytest.raises(ValueError, match="No matching data"):
        entsoe.parse_series('<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><text>No matching data found'
                            '</text></Reason></Acknowledgement_MarketDocument>')


# --- price_series and generation_frame --------------------------------------------------------------------------

def _price(sequence, value):
    return _series([(1, value)], sequence=sequence, price=True, curve="A01")


def test_price_takes_the_one_series_or_sequence_one():
    assert entsoe.price_series(entsoe.parse_series(_document(_price(None, 80))), "CH00").tolist() == [80]
    both = entsoe.parse_series(_document(_price("2", 70), _price("1", 90)))
    assert entsoe.price_series(both, "DE00").tolist() == [90]


@pytest.mark.parametrize("sequences", [("2",), ("2", "3")])
def test_price_without_sequence_one_raises(sequences):
    found = entsoe.parse_series(_document(*[_price(s, 1) for s in sequences]))
    with pytest.raises(ValueError, match="PRICE_SEQUENCE"):
        entsoe.price_series(found, "AT00")


def test_generation_frame_names_the_types_and_rejects_unknown_types_and_consumption():
    found = entsoe.parse_series(_document(_series([(1, 10)], psr="B16"), _series([(1, 5)], psr="B11")))
    assert sorted(entsoe.generation_frame(found).columns) == ["hydro_run_of_river", "solar"]
    with pytest.raises(ValueError, match=r"production types \['B20'\]"):
        entsoe.generation_frame(entsoe.parse_series(_document(_series([(1, 1)], psr="B20"))))
    with pytest.raises(ValueError, match=r"consumption series for \['B10'\]"):
        entsoe.generation_frame(entsoe.parse_series(_document(_series([(1, 1)], psr="B10", consumption=True))))


# --- token ------------------------------------------------------------------------------------------------------

def test_token_comes_from_the_token_file_before_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "api_token"
    path.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv("ENTSOE_API_KEY", "from-env")
    monkeypatch.setattr(entsoe, "_token_file", None)
    assert entsoe.token() == "from-env"
    entsoe.use_token_file(path)
    assert entsoe.token() == "from-file"
    path.write_text("\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="is empty"):
        entsoe.token()
    with pytest.raises(FileNotFoundError, match="does not exist"):
        entsoe.use_token_file(tmp_path / "missing")
    monkeypatch.setattr(entsoe, "_token_file", None)


# --- generation.py: ENTSO-E profiles at BFE monthly totals ------------------------------------------------------

INDEX = hourly_index("2025-10-01", "2025-11-30")  # October 745 hours (autumn DST), November 720


def _balance(**months):
    """BFE balance rows (GWh) for the months given as {"2025-10": {column: value}}."""
    rows = {pd.Timestamp(f"{m}-01"): values for m, values in months.items()}
    return pd.DataFrame.from_dict(rows, orient="index")


def _profiles(solar=None):
    hour = np.arange(len(INDEX))
    return pd.DataFrame({"solar": (hour % 24 >= 8) * (hour % 24 <= 16) * 100.0 if solar is None else solar,
                         "wind_onshore": 10.0, "hydro_run_of_river": 1000.0 + hour % 24, "nuclear": 2000.0},
                        index=INDEX)


BFE_FULL = {"Erzeugung_Photovoltaik_GWh": 90.0, "Erzeugung_Windkraft_GWh": 15.0, "Erzeugung_Laufwerk_GWh": 1100.0,
            "Erzeugung_Kernkraftwerk_GWh": 1400.0, "Erzeugung_Thermische_GWh": 300.0}


def test_each_month_sums_to_bfe_and_keeps_the_entsoe_shape():
    balance = _balance(**{"2025-10": BFE_FULL, "2025-11": {**BFE_FULL, "Erzeugung_Photovoltaik_GWh": 45.0}})
    profiles = _profiles()
    scaled = generation.scale_to_bfe(profiles, balance)
    month = INDEX.tz_convert("Europe/Zurich").strftime("%Y-%m")
    assert scaled["solar"].groupby(month).sum().round(6).tolist() == [90000.0, 45000.0]
    assert scaled["hydro_run_of_river"].groupby(month).sum().round(6).tolist() == [1100000.0, 1100000.0]
    ratio = (scaled["hydro_run_of_river"] / profiles["hydro_run_of_river"]).groupby(month).nunique()
    assert ratio.tolist() == [1, 1]  # one factor per month: the hourly shape is ENTSO-E's


def test_unpublished_or_incomplete_months_stay_nan():
    balance = _balance(**{"2025-10": BFE_FULL})  # November not published
    scaled = generation.scale_to_bfe(_profiles(), balance)
    november = INDEX.tz_convert("Europe/Zurich").month == 11
    assert scaled.loc[november].isna().all().all() and scaled.loc[~november].notna().all().all()
    gap = _profiles()
    gap.iloc[5, gap.columns.get_loc("hydro_run_of_river")] = np.nan  # one October hour missing
    scaled = generation.scale_to_bfe(gap, _balance(**{"2025-10": BFE_FULL, "2025-11": BFE_FULL}))
    assert scaled.loc[~november, "hydro_run_of_river"].isna().all()
    assert scaled.loc[november, "hydro_run_of_river"].notna().all() and scaled.loc[~november, "solar"].notna().all()


def test_bfe_production_without_entsoe_energy_or_missing_columns_raise():
    balance = _balance(**{"2025-10": BFE_FULL, "2025-11": BFE_FULL})
    with pytest.raises(ValueError, match="solar: BFE production but no ENTSO-E energy"):
        generation.scale_to_bfe(_profiles(solar=0.0), balance)
    with pytest.raises(ValueError, match=r"lacks the columns \['nuclear'\]"):
        generation.scale_to_bfe(_profiles().drop(columns="nuclear"), balance)
    with pytest.raises(ValueError, match="BFE balance lacks the columns"):
        generation.scale_to_bfe(_profiles(), balance.drop(columns="Erzeugung_Laufwerk_GWh"))


def test_others_are_bfe_thermal_spread_evenly_over_each_complete_month():
    balance = _balance(**{"2025-10": BFE_FULL, "2025-11": {**BFE_FULL, "Erzeugung_Thermische_GWh": 360.0}})
    others = generation.others_flat(INDEX, balance)
    november = INDEX.tz_convert("Europe/Zurich").month == 11
    assert set(others[~november].round(6)) == {round(300000.0 / 745, 6)}
    assert set(others[november]) == {500.0}
    cut = generation.others_flat(INDEX[24:], balance)  # October lacks its first day
    assert cut[~november[24:]].isna().all() and cut[november[24:]].eq(500.0).all()


def test_build_tables_adds_others_to_both():
    balance = _balance(**{"2025-10": BFE_FULL, "2025-11": BFE_FULL})
    published, scaled = generation.build_tables(_profiles(), balance)
    assert published["solar"].equals(_profiles()["solar"])
    assert published["others"].equals(scaled["others"]) and published["others"].notna().all()


def test_generation_check_compares_complete_published_months_with_bfe():
    balance = _balance(**{"2025-10": BFE_FULL, "2025-11": BFE_FULL})
    frame = pd.DataFrame({"solar": 60000.0 / 745}, index=INDEX[:745 + 24])  # October (60 GWh) and 1 Nov, incomplete
    row = checks.generation_vs_bfe(frame, "test", balance)[0]
    assert (row["item"], row["months_compared"], round(row["mean_a"], 6), row["mean_b"]) == ("solar", 1, 60.0, 90.0)
    assert row["ratio_a_to_b"] == pytest.approx(60.0 / 90.0)


# --- http_get: dropped connections ------------------------------------------------------------------------------

class _Response:
    status, headers = 200, {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return b"<ok/>"


def test_a_dropped_connection_is_retried_then_raises(monkeypatch):
    calls = []

    def flaky(request, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.URLError(ConnectionResetError(10054, "reset"))
        return _Response()

    monkeypatch.setattr(http_get.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(http_get.time, "sleep", lambda s: None)
    body, _ = http_get.get_bytes("https://example.invalid/api?securityToken=secret")
    assert body == b"<ok/>" and len(calls) == 3

    def always(request, timeout):
        raise ConnectionResetError(10054, "reset")

    monkeypatch.setattr(http_get.urllib.request, "urlopen", always)
    with pytest.raises(RuntimeError, match=r"failed after 5 attempts") as caught:
        http_get.get_bytes("https://example.invalid/api?securityToken=secret")
    assert "secret" not in str(caught.value)


def test_ntc_check_counts_values_more_than_half_a_megawatt_apart_where_both_sources_have_one():
    from data_prep.historical.config import DIRECTIONS
    index = INDEX[:4]
    api = pd.DataFrame({d: [1000.0, 1000.0, 1000.0, 1000.0] for d in DIRECTIONS}, index=index)
    other = api.copy()
    other.loc[index[1], "DE_to_CH"] = 1000.4  # within 0.5 MW
    other.loc[index[2], "DE_to_CH"] = 900.0
    other.loc[index[3], "DE_to_CH"] = np.nan  # not compared
    rows = {row["item"]: row for row in checks.ntc_sources(api, other, "test")}
    assert (rows["DE_to_CH"]["values_differing"], rows["DE_to_CH"]["hours_compared"]) == (1, 3)
    assert rows["CH_to_DE"]["values_differing"] == 0 and len(rows) == len(DIRECTIONS)
