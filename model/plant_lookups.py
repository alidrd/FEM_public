"""Lookups the model build computes once instead of inside every constraint rule.

A constraint rule runs once per index, for the curtailment limit once per
consumer, hour and sub-scenario. Anything the rule derives from the plant
lists must therefore be derived here, once, or the build scales with the
square of the plant count (8,556 per-plant PV candidates made it take hours).
"""


def consumer_node_map(map_node_consumer):
    """{consumer: node} from {node: [consumers]}; a consumer listed under two nodes is an error."""
    consumer_node = {}
    for node, consumers in map_node_consumer.items():
        for consumer in consumers:
            if consumer in consumer_node and consumer_node[consumer] != node:
                raise ValueError(
                    f"[plant_lookups] consumer {consumer!r} is listed under nodes "
                    f"{consumer_node[consumer]!r} and {node!r}."
                )
            consumer_node[consumer] = node
    return consumer_node


def res_candidates_by_node(plants, res_candidates, map_node_plant):
    """{node: [plants]} with the RES investment candidates of each node, in the order of `plants`.

    `plants` is the generating plant set (its order is kept, so expressions
    built from the result have the same term order as a scan of `plants`),
    `res_candidates` the Swiss RES candidate list, `map_node_plant`
    {node: [plants at the node]}.
    """
    candidate_set = set(res_candidates)
    by_node = {}
    for node, node_plants in map_node_plant.items():
        at_node = set(node_plants)
        by_node[node] = [p for p in plants if p in candidate_set and p in at_node]
    return by_node
