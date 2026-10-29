"""Unit tests for ENTSO-E actual generation per unit (A73) in data_prep/historical: the request windows and the
download (entsoe.py), the parser (entsoe.parse_unit_series) and the hourly table (process.build_generation_units).
No request is sent."""
import pandas as pd
import pytest

from data_prep.historical import entsoe, process
from data_prep.historical.timeutils import hourly_index

NS = "urn:iec62325.351:tc57wg16:451-6:generationloaddocument:3:0"


def _unit(name, points, eic="12W-0000000001-A", psr="B12", curve="A03", start="2025-10-01T22:00Z",
          end="2025-10-02T02:00Z", resolution="PT60M", consumption=False):
    pts = "".join(f"<Point><position>{p}</position><quantity>{q}</quantity></Point>" for p, q in points)
    domain = ("<outBiddingZone_Domain.mRID>10YCH-SWISSGRIDZ</outBiddingZone_Domain.mRID>" if consumption
              else "<inBiddingZone_Domain.mRID>10YCH-SWISSGRIDZ</inBiddingZone_Domain.mRID>")
    return (f"<TimeSeries>{domain}<curveType>{curve}</curveType><MktPSRType><psrType>{psr}</psrType>"
            f"<PowerSystemResources><mRID codingScheme=\"A01\">{eic}</mRID><name>{name}</name></PowerSystemResources>"
            f"</MktPSRType><Period><timeInterval><start>{start}</start><end>{end}</end></timeInterval>"
            f"<resolution>{resolution}</resolution>{pts}</Period></TimeSeries>")


def _document(*series):
    return f'<GL_MarketDocument xmlns="{NS}">{"".join(series)}</GL_MarketDocument>'


def _acknowledgement(reason):
    return (f'<Acknowledgement_MarketDocument xmlns="urn:x"><Reason><text>{reason}</text></Reason>'
            f'</Acknowledgement_MarketDocument>')


# --- parse_unit_series --------------------------------------------------------------------------------------------

def test_units_are_keyed_by_name_eic_and_type_and_a03_blocks_hold():
    found = entsoe.parse_unit_series(_document(
        _unit("Usine A", [(1, 10), (3, 30)], eic="12W-A"),
        _unit("Kraftwerk B", [(1, 5), (2, 6), (3, 7), (4, 8)], eic="12W-B", psr="B10", curve="A01")))
    assert set(found) == {("Usine A", "12W-A", "B12"), ("Kraftwerk B", "12W-B", "B10")}
    assert found[("Usine A", "12W-A", "B12")].tolist() == [10, 10, 30, 30]
    assert found[("Kraftwerk B", "12W-B", "B10")].index[0] == pd.Timestamp("2025-10-01 22:00", tz="UTC")


def test_periods_of_one_unit_are_joined_and_a_stamp_given_twice_raises():
    first = _unit("Usine A", [(1, 1), (2, 2)], end="2025-10-02T00:00Z")
    second = _unit("Usine A", [(1, 3), (2, 4)], start="2025-10-02T00:00Z")
    assert entsoe.parse_unit_series(_document(first, second))[("Usine A", "12W-0000000001-A", "B12")].tolist() == [
        1, 2, 3, 4]
    with pytest.raises(ValueError, match="two values for"):
        entsoe.parse_unit_series(_document(first, first))


def test_no_data_acknowledgement_gives_nothing_and_any_other_raises():
    assert entsoe.parse_unit_series(_acknowledgement("No matching data found for Data item ... [16.1.A]")) == {}
    with pytest.raises(ValueError, match="ENTSO-E returned no data: The amount of requested data exceeds"):
        entsoe.parse_unit_series(_acknowledgement("The amount of requested data exceeds allowed limit"))


@pytest.mark.parametrize("document, message", [
    (_document(_unit("Usine A", [(1, 1)], consumption=True)), "consumption series of Usine A"),
    (_document(_unit("Usine A", [(1, 1)], curve="A02")), "unsupported curveType A02"),
    (_document(), "no points"),
])
def test_consumption_unknown_curve_and_an_empty_document_raise(document, message):
    with pytest.raises(ValueError, match=message):
        entsoe.parse_unit_series(document)


# --- request windows and download ----------------------------------------------------------------------------------

def test_a_day_is_one_request_and_the_autumn_clock_change_two():
    [(path, start, end)] = entsoe.unit_requests("2025-10-25")
    assert path.name == "A73_CH00_2025-10-25_2025-10-25.xml"
    assert (start, end) == (pd.Timestamp("2025-10-24 22:00", tz="UTC"), pd.Timestamp("2025-10-25 22:00", tz="UTC"))
    long_day = entsoe.unit_requests("2025-10-26")
    assert [p.name for p, _, _ in long_day] == ["A73_CH00_2025-10-26_2025-10-26_h01-24.xml",
                                               "A73_CH00_2025-10-26_2025-10-26_h25.xml"]
    assert [(end - start) / pd.Timedelta(hours=1) for _, start, end in long_day] == [24, 1]
    [(_, start, end)] = entsoe.unit_requests("2026-03-29")  # 23 hours
    assert (end - start) / pd.Timedelta(hours=1) == 23


def test_download_requests_every_window_with_the_control_area(monkeypatch):
    calls = []
    monkeypatch.setattr(entsoe, "request", lambda params, dest, force: calls.append((params, dest.name)) or dest)
    entsoe.download_generation_units("2025-10-25", "2025-10-26", force=False)
    assert [name for _, name in calls] == ["A73_CH00_2025-10-25_2025-10-25.xml",
                                           "A73_CH00_2025-10-26_2025-10-26_h01-24.xml",
                                           "A73_CH00_2025-10-26_2025-10-26_h25.xml"]
    # key order fixes the URL, which the download manifest compares
    assert list(calls[0][0]) == ["documentType", "processType", "in_Domain", "periodStart", "periodEnd"]
    assert calls[0][0] == {"documentType": "A73", "processType": "A16", "in_Domain": "10YCH-SWISSGRIDZ",
                           "periodStart": "202510242200", "periodEnd": "202510252200"}
    assert (calls[2][0]["periodStart"], calls[2][0]["periodEnd"]) == ("202510262200", "202510262300")


# --- the hourly table ----------------------------------------------------------------------------------------------

def _write_day(folder, day, *series):
    [(path, _, _)] = [(p, s, e) for p, s, e in entsoe.unit_requests(day)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_document(*series), encoding="utf-8")


def test_the_table_averages_quarter_hours_lists_the_units_and_leaves_days_without_a_file_nan(monkeypatch, tmp_path):
    monkeypatch.setattr(entsoe, "UNITS_DIR", tmp_path)
    day_1 = "2025-10-01"  # local day: 2025-09-30 22:00 to 2025-10-01 22:00 UTC
    hourly = [(k, 100 + k) for k in range(1, 25)]
    quarter = [(k, k) for k in range(1, 97)]  # hour h holds 4h-3 .. 4h, mean 4h - 1.5
    _write_day(tmp_path, day_1, _unit("Usine A", hourly, eic="12W-A", curve="A01", start="2025-09-30T22:00Z",
                                      end="2025-10-01T22:00Z"),
               _unit("Kraftwerk B", quarter, eic="12W-B", psr="B10", curve="A01", start="2025-09-30T22:00Z",
                     end="2025-10-01T22:00Z", resolution="PT15M"))
    index = hourly_index("2025-10-01", "2025-10-02")
    frame, units, irregular = process.build_generation_units("2025-10-01", "2025-10-02", index)
    assert frame["Usine A"].iloc[:24].tolist() == [101 + k for k in range(24)]
    assert frame["Kraftwerk B"].iloc[:24].tolist() == [4 * h - 1.5 for h in range(1, 25)]
    assert frame.iloc[24:].isna().all().all()  # 2 October was not downloaded
    assert units.to_dict("index") == {"Usine A": {"eic": "12W-A", "psr_type": "B12"},
                                      "Kraftwerk B": {"eic": "12W-B", "psr_type": "B10"}}
    assert len(irregular) == 0


def test_no_file_in_the_period_gives_none_and_two_units_with_one_name_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(entsoe, "UNITS_DIR", tmp_path)
    index = hourly_index("2025-10-01", "2025-10-01")
    assert process.build_generation_units("2025-10-01", "2025-10-01", index) is None
    _write_day(tmp_path, "2025-10-01", _unit("Usine A", [(1, 1)], eic="12W-A"), _unit("Usine A", [(1, 2)], eic="12W-B"))
    with pytest.raises(ValueError, match=r"several units share the names \['Usine A'\]"):
        process.build_generation_units("2025-10-01", "2025-10-01", index)
