"""Model side of a back-cast (docs/backcast.md): the hook that runs after capacity fixing and variable presets,
the checks that reject a model which is not a back-cast model, and the audit of the objective.

The hook runs after the presets so that no preset can undo a back-cast constraint. Constraints a back-cast
adds go through add_backcast_constraint, which registers them for the default dual export.
"""
from pyomo.environ import Constraint, Param, Set
from pyomo.repn import generate_standard_repn

from data_prep.definitions_common import Map_plant_node, Map_plant_tech, Reservoir_end_target, Reservoir_start_stock
from data_prep.historical_import import FIXED_PLANT_TECH, HYDRO_END_MODES
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


def add_reservoir_boundaries(model):
    """Observed boundaries of the open plants: the first hour starts from each plant's start stock instead of the
    cyclic link to the last hour, the start condition is dropped, and the national stock after the last hour meets
    the end target. Both new constraints are unscaled, so their duals are in EUR/MWh."""
    plants = sorted({p for p, _ in Reservoir_start_stock})
    problems = [] if plants else ["no start stock is staged"]
    problems += [f"{p} is no storage plant with inflow" for p in plants if p not in model.P_inflow]
    problems += [f"no start stock of {p} in {s}" for p in plants for s in model.Scenarios
                 if (p, s) not in Reservoir_start_stock]
    problems += [f"no valid end target for {s}" for s in model.Scenarios
                 if Reservoir_end_target.get(s, {}).get("mode") not in HYDRO_END_MODES]
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
    if any(Reservoir_end_target[s]["mode"] != "free" for s in model.Scenarios):
        add_backcast_constraint(model, "reservoir_end_target", Constraint(model.Scenarios, rule=reservoir_end_rule))


def reservoir_first_hour_rule(model, p, s):
    """The production storage balance of the first hour, unscaled, with the start stock as the previous stock."""
    stock, flows = storage_balance_terms(model, p, model.T.first(), s)
    return stock == model.reservoir_start_stock[p, s] + flows


def reservoir_end_rule(model, s):
    target = Reservoir_end_target[s]
    if target["mode"] == "free":
        return Constraint.Skip
    national = sum(model.soc[p, model.T.last(), s] for p in model.P_reservoir)
    return national == target["MWh"] if target["mode"] == "equal" else national >= target["MWh"]


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
