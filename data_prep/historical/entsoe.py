"""ENTSO-E Transparency Platform, RESTful API: day-ahead prices of CH and its four neighbours (document type
A44) and actual Swiss generation per production type (A75), one raw XML file per year chunk.

The API token is read at run time and never stored: from the file passed to `download.py --token-file`, or else
from the environment variable ENTSOE_API_KEY. Raw files and the download manifest hold the request URL with the
token redacted (http_get.redact).
"""
import os
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import pandas as pd

from data_prep.historical import http_get
from data_prep.historical.config import ENTSOE_API, ENTSOE_TOKEN_ENV, RAW_DIR, ZONES
from data_prep.historical.timeutils import period_bounds_utc

RAW = RAW_DIR / "entsoe"
PRICE_DIR = RAW / "prices_A44"
GENERATION_DIR = RAW / "generation_A75"
MIN_INTERVAL_S = 2.0  # pause between requests; the API reset a connection at 1 s spacing (2026-10-23)

# Day-ahead price: since 1 Oct 2025 the API returns two A44 series for DE-LU and AT, classificationSequence 1
# and 2. Sequence 1 equals the price Energy-Charts publishes from SMARD (single day-ahead coupling) in every
# hour of Oct 2025 - Sep 2026; sequence 2 differs in nearly every hour (validation_report.csv). The other
# zones publish one series without a sequence.
PRICE_SEQUENCE = "1"
# A75 production types published for CH -> column names, as in Energy-Charts public_power. Swissgrid publishes
# no other type (no thermal or waste generation, no pumped-storage consumption); a new one raises until it is
# named here.
GENERATION_COLUMNS = {
    "B10": "hydro_pumped_storage",
    "B11": "hydro_run_of_river",
    "B12": "hydro_water_reservoir",
    "B14": "nuclear",
    "B16": "solar",
    "B19": "wind_onshore",
}

_token_file: Path | None = None


def use_token_file(path: Path) -> None:
    """Read the token from this file (one line) instead of the environment variable."""
    global _token_file
    if not Path(path).is_file():
        raise FileNotFoundError(f"ENTSO-E token file {path} does not exist")
    _token_file = Path(path)


def token() -> str:
    if _token_file is not None:
        value = _token_file.read_text(encoding="utf-8").strip()
        if not value:
            raise RuntimeError(f"ENTSO-E token file {_token_file} is empty")
        return value
    value = os.environ.get(ENTSOE_TOKEN_ENV, "").strip()
    if not value:
        raise RuntimeError(
            f"ENTSO-E downloads need an API token: pass --token-file, or set the environment variable "
            f"{ENTSOE_TOKEN_ENV}. Register on transparency.entsoe.eu and email transparency@entsoe.eu "
            "(subject 'Restful API access') to get one."
        )
    return value


def request(params: dict, dest: Path, force: bool) -> Path:
    url = f"{ENTSOE_API}?{urllib.parse.urlencode({**params, 'securityToken': token()})}"
    return http_get.download(url, dest, force, min_interval_s=MIN_INTERVAL_S, host_key="entsoe")


def _bounds(start_day: str, end_day: str) -> dict:
    start, end = period_bounds_utc(start_day, end_day)
    return {"periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M")}


def price_path(node: str, start_day: str, end_day: str) -> Path:
    return PRICE_DIR / f"A44_{node}_{start_day}_{end_day}.xml"


def generation_path(start_day: str, end_day: str) -> Path:
    return GENERATION_DIR / f"A75_CH00_{start_day}_{end_day}.xml"


def download_price(node: str, start_day: str, end_day: str, force: bool) -> Path:
    """Day-ahead price of one FEM node's bidding zone, local days start_day..end_day, at most one year."""
    eic = ZONES[node]["entsoe"]
    params = {"documentType": "A44", "in_Domain": eic, "out_Domain": eic, **_bounds(start_day, end_day)}
    return request(params, price_path(node, start_day, end_day), force)


def download_generation(start_day: str, end_day: str, force: bool) -> Path:
    """Actual Swiss generation per production type (realised, A16), at most one year."""
    params = {"documentType": "A75", "processType": "A16", "in_Domain": ZONES["CH00"]["entsoe"],
              **_bounds(start_day, end_day)}
    return request(params, generation_path(start_day, end_day), force)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_series(xml_text: str) -> dict[tuple, pd.Series]:
    """Every TimeSeries of a market document, at the published resolution, UTC interval start.

    Key: (psrType, classificationSequence position, direction), None where the series has no such field;
    direction is "consumption" for a series with an outBiddingZone_Domain and "generation" otherwise. Series
    with the same key (e.g. one per day) are joined. Values are the point's quantity or price.amount. Curve A03
    (variable blocks) holds a value until the next listed position. Raises on 'no data' (Acknowledgement), an
    unknown curve type, a stamp given twice for one key, or a document without points.
    """
    root = ET.fromstring(xml_text)
    if _local(root.tag) == "Acknowledgement_MarketDocument":
        reason = " ".join(e.text or "" for e in root.iter() if _local(e.tag) == "text")
        raise ValueError(f"ENTSO-E returned no data: {reason.strip()}")
    points: dict[tuple, dict] = {}
    for series_el in (e for e in root.iter() if _local(e.tag) == "TimeSeries"):
        fields = {_local(e.tag): e.text for e in series_el.iter()
                  if _local(e.tag) in ("psrType", "classificationSequence_AttributeInstanceComponent.position",
                                       "outBiddingZone_Domain.mRID", "curveType")}
        curve = fields.get("curveType", "A01")
        if curve not in ("A01", "A03"):
            raise ValueError(f"unsupported curveType {curve}")
        key = (fields.get("psrType"), fields.get("classificationSequence_AttributeInstanceComponent.position"),
               "consumption" if "outBiddingZone_Domain.mRID" in fields else "generation")
        target = points.setdefault(key, {})
        for period in (e for e in series_el if _local(e.tag) == "Period"):
            children = {_local(c.tag): c for c in period}
            bounds = {_local(c.tag): pd.Timestamp(c.text) for c in children["timeInterval"]}
            step = pd.Timedelta(children["resolution"].text)
            n_steps = int((bounds["end"] - bounds["start"]) / step)
            values = {}
            for point in (c for c in period if _local(c.tag) == "Point"):
                point_fields = {_local(c.tag): c.text for c in point}
                amount = point_fields["quantity"] if "quantity" in point_fields else point_fields["price.amount"]
                values[int(point_fields["position"])] = float(amount)
            positions = range(1, n_steps + 1) if curve == "A03" else sorted(values)
            current = None
            for pos in positions:
                current = values.get(pos, current)
                if current is None:
                    raise ValueError(f"A03 period starting {bounds['start']} has no value at position 1")
                stamp = bounds["start"] + (pos - 1) * step
                if stamp in target:
                    raise ValueError(f"two values for {stamp} in series {key}")
                target[stamp] = current
    if not points:
        raise ValueError("ENTSO-E document has no points")
    out = {}
    for key, values in points.items():
        series = pd.Series(values).sort_index()
        series.index.name = "time_utc"
        out[key] = series
    return out


def price_series(found: dict[tuple, pd.Series], node: str) -> pd.Series:
    """The day-ahead price among the parsed A44 series: the one series of a zone without sequences, or sequence
    PRICE_SEQUENCE where the zone publishes several. Raises on anything else."""
    sequences = sorted({key[1] for key in found}, key=str)
    if sequences == [None]:
        return found[(None, None, "generation")]
    if None in sequences or PRICE_SEQUENCE not in sequences:
        raise ValueError(f"A44 {node}: classification sequences {sequences}; expected none, or one of them "
                         f"{PRICE_SEQUENCE!r} (PRICE_SEQUENCE)")
    return found[(None, PRICE_SEQUENCE, "generation")]


def generation_frame(found: dict[tuple, pd.Series]) -> pd.DataFrame:
    """Parsed A75 series -> one column per production type, named after GENERATION_COLUMNS. Raises on an unnamed
    type and on a consumption series, which this table does not hold."""
    unknown = sorted({key[0] for key in found} - set(GENERATION_COLUMNS), key=str)
    if unknown:
        raise ValueError(f"A75: production types {unknown} are not in GENERATION_COLUMNS; name them first")
    consumption = sorted(key[0] for key in found if key[2] == "consumption")
    if consumption:
        raise ValueError(f"A75: consumption series for {consumption}; this table holds generation only")
    return pd.DataFrame({GENERATION_COLUMNS[key[0]]: series for key, series in found.items()}).sort_index()


def read_price(path: Path, node: str) -> pd.Series:
    return price_series(parse_series(path.read_text(encoding="utf-8")), node)


def read_generation(path: Path) -> pd.DataFrame:
    return generation_frame(parse_series(path.read_text(encoding="utf-8")))
