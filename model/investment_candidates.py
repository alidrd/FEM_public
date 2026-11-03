"""The setting `investment_markets`: which markets may invest in a scenario.

`input/plants_invest_candidates_res_CH.csv` lists candidates in CH00 and in the neighbours
DE00, AT00, FR00 and IT00, and `battery_investment_nodes_in_addition_to_CH` adds batteries in
further nodes. `investment_markets` keeps only the candidates of the listed markets, so a run
set that varies Switzerland does not also vary the neighbours' capacities. False, the default,
keeps every candidate. Missing or contradictory values raise; nothing is dropped silently.
"""
from typing import List, Optional

import pandas as pd

SETTING = "investment_markets"
BATTERY_SETTING = "battery_investment_nodes_in_addition_to_CH"


def parse_market_list(setting) -> Optional[List[str]]:
    """False, None or a blank cell mean every market (None); a comma-separated string or a list gives the codes."""
    if setting is None or setting is False:
        return None
    if isinstance(setting, float):
        if pd.isna(setting):
            return None
        raise TypeError(f"{SETTING} must be False or market codes, got {setting!r}")
    if isinstance(setting, str):
        text = setting.strip()
        if text == "" or text.lower() == "nan":
            return None
        markets = [code.strip() for code in text.split(",") if code.strip()]
    elif isinstance(setting, (list, tuple)):
        markets = [str(code).strip() for code in setting if str(code).strip()]
    else:
        raise TypeError(f"{SETTING} must be False or a comma-separated string of market codes, got {setting!r}")
    if not markets:
        raise ValueError(f"{SETTING} is set but names no market: {setting!r}")
    return markets


def keep_candidates_in_markets(candidates: pd.DataFrame, markets: Optional[List[str]]) -> pd.DataFrame:
    """The candidate rows whose `market` is listed; every listed market must have at least one candidate."""
    if markets is None:
        return candidates
    present = sorted(set(candidates["market"]))
    missing = [code for code in markets if code not in present]
    if missing:
        raise ValueError(
            f"{SETTING} lists {missing}, but no investment candidate has that market "
            f"(markets with candidates: {present})"
        )
    return candidates[candidates["market"].isin(markets)]


def battery_node_market(node: str) -> str:
    """The market of an extra battery node, as add_additional_batteries derives it: 'IT01' -> 'IT00'."""
    return node[:2] + "00"


def check_battery_nodes_in_markets(battery_nodes: List[str], markets: Optional[List[str]]) -> None:
    """Raise when an extra battery node lies outside the kept markets, rather than dropping it silently."""
    if markets is None:
        return
    outside = [node for node in battery_nodes if battery_node_market(node) not in markets]
    if outside:
        raise ValueError(
            f"{BATTERY_SETTING} adds batteries in {outside}, outside {SETTING} {markets}; "
            f"empty that setting or list their markets"
        )
