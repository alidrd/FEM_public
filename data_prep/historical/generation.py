"""Swiss generation per type from ENTSO-E hourly profiles and BFE monthly totals (decided 2026-10-23).

ENTSO-E A75 (Swissgrid) covers only part of Swiss generation: over Oct 2025 - Aug 2026 its solar is 63 % of
BFE's monthly statistics, run of river 84 %, nuclear and wind 100-106 %, and it has no thermal or waste
generation at all (validation_report.csv). Its hourly profiles are measured, though; run of river, for example,
is nearly flat within a day. Two tables are built from it:

- ch_generation_entsoe_hourly.csv: the ENTSO-E series as published;
- ch_generation_entsoe_bfe_hourly.csv: each ENTSO-E profile scaled so that every local calendar month sums to BFE's
  production of that type (BFE_GENERATION).

Both carry `others`, which ENTSO-E does not publish for CH: BFE's thermal production, spread evenly over the hours of
each month (the same shape Energy-Charts gives its series `others`). BFE's column `Erzeugung_andere_GWh` is empty
(NULL) in every month since at least 2023; national production is the sum of the other six types. A month cut by the
start or end of the period, missing an hour, or not yet published by BFE stays NaN; nothing is filled.
"""
import pandas as pd

from data_prep.historical.demand import calendar_month_hours, local_month

# column -> BFE monthly balance column(s) whose sum is that type's production
BFE_GENERATION = {
    "solar": ("Erzeugung_Photovoltaik_GWh",),
    "wind_onshore": ("Erzeugung_Windkraft_GWh",),
    "hydro_run_of_river": ("Erzeugung_Laufwerk_GWh",),
    "nuclear": ("Erzeugung_Kernkraftwerk_GWh",),
}
BFE_OTHERS = ("Erzeugung_Thermische_GWh",)


def _bfe_monthly(balance: pd.DataFrame, columns: tuple[str, ...], months: pd.DatetimeIndex) -> pd.Series:
    missing = [c for c in columns if c not in balance.columns]
    if missing:
        raise ValueError(f"BFE balance lacks the columns {missing}")
    return balance.reindex(months)[list(columns)].sum(axis=1, min_count=len(columns))


def _complete_months(index: pd.DatetimeIndex, present: pd.Series) -> tuple[pd.DatetimeIndex, pd.Series]:
    """Local month of every hour, and per month whether the index holds all its hours with present values."""
    month = local_month(index)
    hours = calendar_month_hours(pd.DatetimeIndex(month.unique()))
    return month, present.groupby(month).sum().eq(hours)


def others_flat(index: pd.DatetimeIndex, balance: pd.DataFrame) -> pd.Series:
    """BFE thermal plus other production (MW), spread evenly over the hours of each complete, published month."""
    month, complete = _complete_months(index, pd.Series(True, index=index))
    monthly = _bfe_monthly(balance, BFE_OTHERS, complete.index).where(complete)
    hours = calendar_month_hours(complete.index)
    return pd.Series((monthly * 1000 / hours).reindex(month).to_numpy(), index=index, name="others")


def scale_to_bfe(profiles: pd.DataFrame, balance: pd.DataFrame) -> pd.DataFrame:
    """The columns of BFE_GENERATION, each scaled per local month so that its energy equals BFE's production.

    A month gets values only if every one of its hours has an ENTSO-E value and BFE has published the month.
    Raises if a column is missing, or if a month has BFE production but no ENTSO-E energy to scale.
    """
    missing = [c for c in BFE_GENERATION if c not in profiles.columns]
    if missing:
        raise ValueError(f"ENTSO-E generation lacks the columns {missing}")
    out = {}
    for column, bfe_columns in BFE_GENERATION.items():
        series = profiles[column]
        month, complete = _complete_months(series.index, series.notna())
        bfe_gwh = _bfe_monthly(balance, bfe_columns, complete.index).where(complete)
        entsoe_gwh = series.groupby(month).sum() / 1000
        empty = bfe_gwh.notna() & (bfe_gwh > 0) & ~(entsoe_gwh > 0)
        if empty.any():
            raise ValueError(f"{column}: BFE production but no ENTSO-E energy in {list(empty.index[empty].date)}")
        factor = (bfe_gwh / entsoe_gwh).where(bfe_gwh.notna())
        out[column] = series * factor.reindex(month).to_numpy()
    return pd.DataFrame(out, index=profiles.index)


def build_tables(profiles: pd.DataFrame, balance: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(ENTSO-E as published, ENTSO-E scaled to BFE months), both with `others` from BFE."""
    others = others_flat(profiles.index, balance)
    published = profiles.assign(others=others)
    scaled = scale_to_bfe(profiles, balance).assign(others=others)
    return published, scaled
