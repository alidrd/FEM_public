"""Unit tests for model/investment_candidates.py, the `investment_markets` setting."""
import math

import pandas as pd
import pytest

from model.investment_candidates import (
    battery_node_market,
    check_battery_nodes_in_markets,
    keep_candidates_in_markets,
    parse_market_list,
)


def candidates():
    return pd.DataFrame(
        {"market": ["CH00", "CH00", "DE00", "IT00", "AT00"], "tech": ["pvrf", "windon", "pvrf", "windon", "CCGTCCS"]},
        index=["CH01_pvrf", "CH01_windon", "DE01_pvrf", "IT01_windon", "AT00_newCCGTCCS"],
    )


@pytest.mark.parametrize("setting", [False, None, "", "  ", "nan", "NaN", math.nan])
def test_parse_market_list_treats_off_values_as_every_market(setting):
    assert parse_market_list(setting) is None


def test_parse_market_list_splits_a_comma_separated_string():
    assert parse_market_list(" CH00, DE00 ,,") == ["CH00", "DE00"]


def test_parse_market_list_accepts_a_list():
    assert parse_market_list(["CH00", "AT00"]) == ["CH00", "AT00"]


@pytest.mark.parametrize("setting", [1, 2.5, True])
def test_parse_market_list_rejects_numbers_and_true(setting):
    with pytest.raises(TypeError, match="investment_markets must be False"):
        parse_market_list(setting)


def test_parse_market_list_rejects_a_list_without_codes():
    with pytest.raises(ValueError, match="names no market"):
        parse_market_list([" ", ""])


def test_keep_candidates_in_markets_keeps_only_the_listed_markets_in_file_order():
    kept = keep_candidates_in_markets(candidates(), ["CH00"])
    assert list(kept.index) == ["CH01_pvrf", "CH01_windon"]
    kept = keep_candidates_in_markets(candidates(), ["AT00", "CH00"])
    assert list(kept.index) == ["CH01_pvrf", "CH01_windon", "AT00_newCCGTCCS"]


def test_keep_candidates_in_markets_returns_everything_when_off():
    assert keep_candidates_in_markets(candidates(), None).equals(candidates())


def test_keep_candidates_in_markets_rejects_a_market_without_candidates():
    with pytest.raises(ValueError, match=r"lists \['CH0'\].*markets with candidates: \['AT00', 'CH00', 'DE00', 'IT00'\]"):
        keep_candidates_in_markets(candidates(), ["CH00", "CH0"])


def test_battery_node_market_maps_the_sub_node_to_its_market():
    assert battery_node_market("IT01") == "IT00"
    assert battery_node_market("CH00") == "CH00"


def test_check_battery_nodes_in_markets_passes_when_off_or_inside():
    check_battery_nodes_in_markets(["AT01", "DE01"], None)
    check_battery_nodes_in_markets([], ["CH00"])
    check_battery_nodes_in_markets(["AT01"], ["CH00", "AT00"])


def test_check_battery_nodes_in_markets_rejects_a_battery_outside_the_markets():
    with pytest.raises(ValueError, match=r"batteries in \['AT01', 'IT01'\], outside investment_markets \['CH00'\]"):
        check_battery_nodes_in_markets(["AT01", "IT01"], ["CH00"])
