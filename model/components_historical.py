"""Model side of a back-cast (docs/backcast.md): the hook that runs after capacity fixing and variable presets,
the checks that reject a model which is not a back-cast model, and the audit of the objective.

The hook runs after the presets so that no preset can undo a back-cast constraint. Constraints a back-cast
adds go through add_backcast_constraint, which registers them for the default dual export.
"""
from pyomo.environ import Constraint, Param, Set, value
from pyomo.repn import generate_standard_repn

from data_prep.definitions_common import (Hydro_fixed_group, Hydro_fixed_output, Hydro_operation, Hydro_outage_cap,
                                          Map_plant_node, Map_plant_tech, Reservoir_end_target, Reservoir_group,
                                          Reservoir_path_target, Reservoir_start_stock)
from data_prep.historical_import import FIXED_PLANT_TECH, HYDRO_END_MODES, HYDRO_TECHS
from model.components_common import storage_balance_terms
from model.variable_presets import read_variable_presets

NODE = "CH00"
# Technologies of the back-cast fleet: the representative hydro fleet and the fixed plants
BACKCAST_TECHS = ["psp_open", "psp_close", "dam", *FIXED_PLANT_TECH.values()]
INVESTMENT_VARIABLES = ("gen_max", "pmp_max", "gen_energy_max", "genTh_max", "pumpTh_max", "gen_energyTh_max",
                        "fuel_storage_capacity_annual")
# Fuel and energy budgets, and the electrolyzer windows (one constraint per plant: consume_tot_limit_<plant>_...)
BUDGET_CONSTRAINTS = ("energy_limit", "storage_total_fuel_limit", "fuel_consumption_tracking",
                      "plant_fuel_consumption_tracking_CH", "plant_fuel_consumption_tracking_CH_DH", "fuel_limit_annual",
                      "fuel_storage_capacity_annual_equal", "fuel_storage_capacity_annual_investment_limit")
BUDGET_PREFIXES = ("consume_tot_limit_",)
SWITCHED_OFF_CONSTRAINTS = ("Constraint_winter_limit", "Constraint_investment_res_CH")
# Parts built on the static calendar (days, weeks), which back-cast model steps do not follow
CALENDAR_CONSTRAINTS = ("energy_shift_limit_dsr_daily", "dsr_daily_balance", "ev_consumption_weekly_sum",
                        "ev_consumption_hourly_rate", "building_heat_demand", "building_weekly_average",
                        "max_heating_capacity", "dsrth_thermal_energy_dev_tracking", "dsrTh_dev_limit",
                        "dsrTh_dev_week_start_zero", "dsrTh_dev_week_end_zero", "dsrTh_dev_weekly_average_temp")
# Objective terms and their currency; plan section 8
OBJECTIVE_TERMS = {
    "cost_inv_dict": "CHF",
    "cost_inv_thermal_dict": "CHF",
    "cost_inv_fuel_storage_dict": "CHF",
    "cost_op_dict": "EUR",
    "cost_op_thermal_dict": "CHF",
    "lostload_cost_dict": "EUR",
    "trade_cost_dict": "EUR",
    "slack_cost_dict": "penalty",
}
INVESTMENT_TERMS = ("cost_inv_dict", "cost_inv_thermal_dict", "cost_inv_fuel_storage_dict")


def apply_historical_constraints(model, scenario_name, preset_file):
    """The back-cast hook: check the built model, add the back-cast constraints and audit the objective."""
    check_historical_model(model, scenario_name, preset_file)
    add_fixed_generation(model)
    add_reservoir_boundaries(model)
    add_hydro_operation(model)
    add_fixed_hydro_output(model)
    for line in audit_objective(model):
        print("objective audit:", line)


def add_fixed_generation(model):
    """Output of each fixed plant equals its observed series: gen == gen_max * avail_plant, which the import sets to
    P_base and observed / P_base. Raises if a fixed plant is no generating plant or has an energy or fuel limit."""
    plants = list(FIXED_PLANT_TECH)
    problems = [f"{p} is no generating plant of the model" for p in plants if p not in model.P_gen]
    problems += [f"{p} has an energy limit" for p in plants if p in model.P_energylim]
    problems += [f"{p} has a fuel limit" for p in plants if p in model.P_fuellimCH]
    if problems:
        raise ValueError("fixed plants: " + "; ".join(problems))
    model.P_fixed = Set(initialize=plants, within=model.P_gen, doc="fixed plants of a back-cast")
    add_backcast_constraint(model, "fixed_generation",
                            Constraint(model.P_fixed, model.T, model.Scenarios, rule=fixed_generation_rule))


def fixed_generation_rule(model, p, t, s):
    return model.gen[p, t, s] == model.gen_max[p, s] * model.avail_plant[p, t, s]


def add_fixed_hydro_output(model):
    """Output of each plant group of Hydro_fixed_output equals its observed output in the hours it is published
    (historical_hydro_fixed_units, a diagnostic); unscaled, so the dual is in EUR/MWh. Raises if a group's plant is no
    storage-hydro plant of the model or a step is no model step."""
    if not Hydro_fixed_output:
        return
    hydro = [p for p in model.P_gen if Map_plant_tech[p] in HYDRO_TECHS]
    problems = [f"group {g}: {p} is no storage-hydro plant of the model" for g, plants in Hydro_fixed_group.items()
                for p in plants if p not in hydro]
    problems += [f"fixed output of {g} for unknown group" for g in {g for g, _, _ in Hydro_fixed_output}
                 if g not in Hydro_fixed_group]
    problems += [f"fixed output step {t} is no model step" for t in {t for _, t, _ in Hydro_fixed_output}
                 if t not in model.T]
    if problems:
        raise ValueError("fixed hydro output: " + "; ".join(problems))
    model.G_hydro_fixed = Set(initialize=sorted(Hydro_fixed_group), doc="plant groups with observed output")
    add_backcast_constraint(model, "hydro_fixed_output", Constraint(
        model.G_hydro_fixed, model.T, model.Scenarios, rule=hydro_fixed_output_rule))


def hydro_fixed_output_rule(model, g, t, s):
    if (g, t, s) not in Hydro_fixed_output:
        return Constraint.Skip
    return sum(model.gen[p, t, s] for p in Hydro_fixed_group[g]) == Hydro_fixed_output[(g, t, s)]


def add_reservoir_boundaries(model):
    """Observed boundaries of the open plants: the first hour starts from each plant's start stock instead of the
    cyclic link to the last hour, the start condition is dropped, and the stock of each reservoir group after the last
    hour meets its end target; with path targets, also after each target step. The new constraints are unscaled, so
    their duals are in EUR/MWh."""
    plants = sorted({p for p, _ in Reservoir_start_stock})
    groups = list(dict.fromkeys(Reservoir_group[p] for p in plants if p in Reservoir_group))
    path_steps = list(dict.fromkeys(t for t, _, _ in Reservoir_path_target))
    problems = [] if plants else ["no start stock is staged"]
    problems += [f"{p} is no storage plant with inflow" for p in plants if p not in model.P_inflow]
    problems += [f"{p} has no reservoir group" for p in plants if p not in Reservoir_group]
    problems += [f"no start stock of {p} in {s}" for p in plants for s in model.Scenarios
                 if (p, s) not in Reservoir_start_stock]
    problems += [f"no valid end target for {s}" for s in model.Scenarios
                 if Reservoir_end_target.get(s, {}).get("mode") not in HYDRO_END_MODES]
    problems += [f"no end target of group {g} in {s}" for s in model.Scenarios for g in groups
                 if Reservoir_end_target.get(s, {}).get("mode") in ("equal", "at_least")
                 and g not in (Reservoir_end_target[s]["MWh"] or {})]
    problems += [f"path target step {t} is no model step" for t in path_steps if t not in model.T]
    problems += [f"no path target of group {g} in {s} at {t}" for t in path_steps for g in groups for s in model.Scenarios
                 if (t, g, s) not in Reservoir_path_target]
    if problems:
        raise ValueError("reservoir boundaries: " + "; ".join(problems))
    for p in plants:
        for s in model.Scenarios:
            model.storage_soc[p, model.T.first(), s].deactivate()
            model.storage_start_condition[p, s].deactivate()
    model.P_reservoir = Set(initialize=plants, within=model.P_storage, doc="open plants with observed boundaries")
    model.reservoir_start_stock = Param(model.P_reservoir, model.Scenarios,
                                        initialize={key: Reservoir_start_stock[key] for key in Reservoir_start_stock},
                                        doc="observed stock before the first hour (MWh)")
    add_backcast_constraint(model, "reservoir_first_hour",
                            Constraint(model.P_reservoir, model.Scenarios, rule=reservoir_first_hour_rule))
    model.Reservoir_groups = Set(initialize=groups, ordered=True, doc="reservoir groups whose stocks meet targets")
    if any(Reservoir_end_target[s]["mode"] != "free" for s in model.Scenarios):
        add_backcast_constraint(model, "reservoir_end_target",
                                Constraint(model.Reservoir_groups, model.Scenarios, rule=reservoir_end_rule))
    if path_steps:
        model.T_reservoir_path = Set(initialize=path_steps, within=model.T, ordered=True,
                                     doc="steps after which each group's stock meets a path target")
        add_backcast_constraint(model, "reservoir_path_target",
                                Constraint(model.T_reservoir_path, model.Reservoir_groups, model.Scenarios,
                                           rule=reservoir_path_rule))


def group_stock(model, g, t, s):
    return sum(model.soc[p, t, s] for p in model.P_reservoir if Reservoir_group[p] == g)


def reservoir_path_rule(model, t, g, s):
    return group_stock(model, g, t, s) == Reservoir_path_target[t, g, s]


def reservoir_first_hour_rule(model, p, s):
    """The production storage balance of the first hour, unscaled, with the start stock as the previous stock."""
    stock, flows = storage_balance_terms(model, p, model.T.first(), s)
    return stock == model.reservoir_start_stock[p, s] + flows


def reservoir_end_rule(model, g, s):
    target = Reservoir_end_target[s]
    if target["mode"] == "free":
        return Constraint.Skip
    stock = group_stock(model, g, model.T.last(), s)
    return stock == target["MWh"][g] if target["mode"] == "equal" else stock >= target["MWh"][g]


def add_hydro_operation(model):
    """Storage-hydro operating constraints (docs/backcast.md, section Storage-hydro operation), each only if staged: a
    must-run floor on total storage-hydro output, an upward reserve withheld from the available turbine capacity
    C(t) (gen_max x avail_plant, capped by outages), and per-plant outage caps on generation and pumping. Raises
    before solving if the floor exceeds C(t) or C(t) minus the reserve. All rows unscaled: duals in EUR/MWh."""
    plants = [p for p in model.P_gen if Map_plant_tech[p] in HYDRO_TECHS]
    problems = [f"no hydro operation is staged for {s}" for s in model.Scenarios if s not in Hydro_operation]
    problems += [f"outage of {p}, no storage-hydro plant of the model" for p in {p for p, _, _ in Hydro_outage_cap}
                 if p not in plants]
    problems += [f"outage step {t} is no model step" for t in {t for _, t, _ in Hydro_outage_cap} if t not in model.T]
    if problems:
        raise ValueError("hydro operation: " + "; ".join(problems))
    capacity = {}
    if any(Hydro_operation[s]["floor_MW"] is not None or Hydro_operation[s]["reserve_up_MW"] is not None
           for s in model.Scenarios):
        capacity = {(t, s): available_turbine_capacity(model, plants, t, s) for t in model.T for s in model.Scenarios}
    for s in model.Scenarios:
        floor, reserve = Hydro_operation[s]["floor_MW"], Hydro_operation[s]["reserve_up_MW"]
        for t in model.T:
            if floor is not None and floor > capacity[t, s]:
                raise ValueError(f"hydro operation: the floor of {floor} MW exceeds the available storage-hydro turbine "
                                 f"capacity of {capacity[t, s]:.1f} MW in {t} of {s}")
            if reserve is not None and capacity[t, s] - reserve < (floor or 0):
                raise ValueError(f"hydro operation: available capacity minus the upward reserve, "
                                 f"{capacity[t, s] - reserve:.1f} MW, is below the floor of "
                                 f"{floor if floor is not None else 0} MW in {t} of {s}")
    if capacity:
        model.P_hydro_operation = Set(initialize=plants, within=model.P_gen, doc="storage-hydro plants of a back-cast")
    if any(Hydro_operation[s]["floor_MW"] is not None for s in model.Scenarios):
        add_backcast_constraint(model, "hydro_reserve_floor",
                                Constraint(model.T, model.Scenarios, rule=hydro_reserve_floor_rule))
    if any(Hydro_operation[s]["reserve_up_MW"] is not None for s in model.Scenarios):
        model.hydro_reserve_up_limit = Param(
            model.T, model.Scenarios, doc="available storage-hydro turbine capacity minus the upward reserve (MW)",
            initialize={(t, s): capacity[t, s] - Hydro_operation[s]["reserve_up_MW"] for t in model.T
                        for s in model.Scenarios if Hydro_operation[s]["reserve_up_MW"] is not None})
        add_backcast_constraint(model, "hydro_reserve_up",
                                Constraint(model.T, model.Scenarios, rule=hydro_reserve_up_rule))
    if Hydro_outage_cap:
        model.P_hydro_outage = Set(initialize=sorted({p for p, _, _ in Hydro_outage_cap}), within=model.P_gen,
                                   doc="storage-hydro plants with an outage or limit")
        add_backcast_constraint(model, "hydro_outage_turbine", Constraint(
            model.P_hydro_outage, model.T, model.Scenarios, rule=hydro_outage_turbine_rule))
        if any(p in model.P_pumping for p in model.P_hydro_outage):
            add_backcast_constraint(model, "hydro_outage_pump", Constraint(
                model.P_hydro_outage, model.T, model.Scenarios, rule=hydro_outage_pump_rule))


def available_turbine_capacity(model, plants, t, s):
    """C(t): the storage-hydro plants' gen_max x avail_plant, each capped by an outage in that step."""
    return sum(min(value(model.gen_max[p, s]) * value(model.avail_plant[p, t, s]),
                   Hydro_outage_cap[(p, t, s)][0] if (p, t, s) in Hydro_outage_cap else float("inf")) for p in plants)


def hydro_reserve_floor_rule(model, t, s):
    floor = Hydro_operation[s]["floor_MW"]
    if floor is None:
        return Constraint.Skip
    return sum(model.gen[p, t, s] for p in model.P_hydro_operation) >= floor


def hydro_reserve_up_rule(model, t, s):
    if Hydro_operation[s]["reserve_up_MW"] is None:
        return Constraint.Skip
    return sum(model.gen[p, t, s] for p in model.P_hydro_operation) <= model.hydro_reserve_up_limit[t, s]


def hydro_outage_turbine_rule(model, p, t, s):
    if (p, t, s) not in Hydro_outage_cap:
        return Constraint.Skip
    return model.gen[p, t, s] <= Hydro_outage_cap[(p, t, s)][0]


def hydro_outage_pump_rule(model, p, t, s):
    if (p, t, s) not in Hydro_outage_cap or p not in model.P_pumping:
        return Constraint.Skip
    return model.storage_charge[p, t, s] <= Hydro_outage_cap[(p, t, s)][1]


def add_backcast_constraint(model, name, constraint):
    """Add a back-cast constraint and register it for the default dual export."""
    model.add_component(name, constraint)
    if not hasattr(model, "backcast_constraint_names"):
        model.backcast_constraint_names = []
    model.backcast_constraint_names.append(name)


def registered_constraints(model):
    """Names of the constraints added through the back-cast hook; none in other runs."""
    return list(model.backcast_constraint_names) if hasattr(model, "backcast_constraint_names") else []


def check_historical_model(model, scenario_name, preset_file):
    """Raise, naming every reason, if the built model is not a back-cast model."""
    problems = (plant_problems(model) + free_investment_problems(model)
                + active_constraint_problems(model, BUDGET_CONSTRAINTS + SWITCHED_OFF_CONSTRAINTS + CALENDAR_CONSTRAINTS,
                                             BUDGET_PREFIXES)
                + switch_problems(model) + preset_problems(scenario_name, preset_file))
    if problems:
        raise ValueError(f"{scenario_name} is not a back-cast model: " + "; ".join(problems))


def plant_problems(model):
    problems = [f"plant {p} ({Map_plant_tech[p]}, {Map_plant_node[p]}) is outside the back-cast fleet"
                for p in model.P if Map_plant_tech[p] not in BACKCAST_TECHS or Map_plant_node[p] != NODE]
    if len(model.PDH):
        problems.append(f"district-heating plants {sorted(model.PDH)[:3]} are built")
    return problems


def free_investment_problems(model):
    problems = []
    for name in INVESTMENT_VARIABLES:
        variable = model.component(name)
        free = [index for index in variable if not variable[index].fixed] if variable is not None else []
        if free:
            problems.append(f"investment variable {name} is free for {free[:3]}")
    return problems


def active_constraint_problems(model, names, prefixes):
    problems = []
    for constraint in model.component_objects(Constraint, active=True):
        if constraint.name in names or constraint.name.startswith(prefixes):
            rows = [index for index in constraint if constraint[index].active]
            if rows:
                problems.append(f"constraint {constraint.name} is active ({len(rows)} rows)")
    return problems


def switch_problems(model):
    problems = []
    if len(model.P_dsr):
        problems.append(f"demand-side response plants {sorted(model.P_dsr)[:3]} are built")
    if getattr(model, "slack_soc", False):
        problems.append("slack_soc is on")
    return problems


def preset_problems(scenario_name, preset_file):
    applicable = [p for p in read_variable_presets(preset_file) if p["scenario_name"] in ("", scenario_name)]
    return [f"{len(applicable)} variable presets apply to {scenario_name}, e.g. {applicable[0]['variable_name']}"] if applicable else []


def audit_objective(model):
    """One line per objective term group with its unit; raises on an active CHF investment or fixed-cost term.

    A term is active if it has a variable with a non-zero coefficient; a term whose variables are all fixed is a
    constant offset (for example the costs of fixed plants). An investment term with all coefficients zero is
    inactive.
    """
    lines, problems = [], []
    for name, unit in OBJECTIVE_TERMS.items():
        terms = getattr(model, name, {})
        active, offset = 0, 0.0
        for expression in terms.values():
            repn = generate_standard_repn(expression) if not isinstance(expression, (int, float)) else None
            coefficients = [c for c in repn.linear_coefs if c != 0] if repn is not None else []
            active += bool(coefficients) or bool(repn is not None and repn.nonlinear_expr is not None)
            offset += repn.constant if repn is not None else expression
        if name in INVESTMENT_TERMS and (active or offset):
            problems.append(f"{name} ({unit}) has {active} active terms and a constant {offset}")
        lines.append(f"{name} ({unit}): {active} active terms, constant offset {offset}")
    if problems:
        raise ValueError("a back-cast has no investment cost, but " + "; ".join(problems))
    return lines
