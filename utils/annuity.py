"""Annuity of an overnight investment cost, FEM's single financing rule.

`utils/data_import_costs.get_cost_component` applies it to every technology
of `input/cost_assumptions.xlsx`; `model/pv_roof_costs` applies it to the
size-class costs of per-plant rooftop PV. Keep the two on this one function.
"""

DISCOUNT_RATE = 0.05  # per year, the social discount rate FEM uses for every technology


def annuitise(investment_cost, amortization_years, discount_rate=DISCOUNT_RATE):
    """Yearly cost of `investment_cost` paid off over `amortization_years`.

    Yearly cost = investment * r / (1 - (1 + r) ** -n). With no amortization
    period or no cost the overnight cost (or 0) is returned unchanged, exactly
    as `get_cost_component` has always done.
    """
    if amortization_years > 0 and investment_cost:
        return investment_cost * (discount_rate / (1 - (1 + discount_rate) ** (-amortization_years)))
    return investment_cost if investment_cost else 0
