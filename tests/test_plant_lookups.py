"""Unit tests for model/plant_lookups.py, the lookups the model build computes once."""
import pytest

from model.plant_lookups import consumer_node_map, res_candidates_by_node


def test_consumer_node_map_inverts_the_node_to_consumers_map():
    node_consumers = {"CH00": ["CH00_fixedconsumer", "ID1"], "DE00": ["DE00_fixedconsumer"], "AT00": []}
    assert consumer_node_map(node_consumers) == {
        "CH00_fixedconsumer": "CH00", "ID1": "CH00", "DE00_fixedconsumer": "DE00",
    }


def test_consumer_node_map_rejects_a_consumer_under_two_nodes():
    with pytest.raises(ValueError, match="'ID1' is listed under nodes 'CH00' and 'DE00'"):
        consumer_node_map({"CH00": ["ID1"], "DE00": ["ID1"]})


def test_res_candidates_by_node_filters_by_both_lists_and_keeps_plant_order():
    plants = ["CH00_z_pvrf", "CH00_a_pvrf", "DE01_pvrf", "CH00_gas", "CH01_windon"]
    res_candidates = ["CH00_a_pvrf", "CH00_z_pvrf", "CH01_windon", "CH00_not_a_plant"]
    map_node_plant = {"CH00": ["CH00_gas", "CH00_a_pvrf", "CH00_z_pvrf", "CH01_windon"], "DE00": ["DE01_pvrf"], "AT00": []}

    by_node = res_candidates_by_node(plants, res_candidates, map_node_plant)

    assert by_node == {
        "CH00": ["CH00_z_pvrf", "CH00_a_pvrf", "CH01_windon"],   # order of `plants`, not of the lists
        "DE00": [],                                                 # DE01_pvrf is a plant but not a candidate
        "AT00": [],
    }


def test_res_candidates_by_node_matches_the_rule_it_replaces():
    """Same result as the per-constraint scan `[p for p in plants if p in res_candidates if p in map[node]]`."""
    plants = [f"p{i}" for i in range(40)]
    res_candidates = [f"p{i}" for i in range(0, 40, 3)]
    map_node_plant = {"n1": [f"p{i}" for i in range(0, 20)], "n2": [f"p{i}" for i in range(15, 40)]}

    by_node = res_candidates_by_node(plants, res_candidates, map_node_plant)

    for node in map_node_plant:
        assert by_node[node] == [p for p in plants if p in res_candidates if p in map_node_plant[node]]
