"""Unit tests for utils/annuity.py, FEM's one annuity rule, and its use by the cost import."""
import pytest

from utils.annuity import DISCOUNT_RATE, annuitise


def test_annuitise_matches_the_annuity_formula():
    rate, years = 0.05, 25
    assert annuitise(615_000, years, rate) == pytest.approx(615_000 * rate / (1 - (1 + rate) ** -years))
    assert annuitise(1_000_000, 25) == pytest.approx(70_952.46, rel=1e-6)   # the default rate is 5 %
    assert DISCOUNT_RATE == 0.05


def test_annuitise_without_a_period_or_a_cost_returns_the_overnight_cost_or_zero():
    assert annuitise(500.0, 0) == 500.0
    assert annuitise(0, 25) == 0
    assert annuitise(None, 25) == 0


def test_get_cost_component_prices_pvrf_with_the_shared_helper():
    """The technology path and the cost-class path must share one financing rule."""
    from input.cost_operation_invest_data import amortization_years_all, cost_component
    from scenarios.scenarios import scenarios_list
    from utils.data_import_costs import get_cost_component

    scenario = next(iter(scenarios_list))
    overnight = cost_component["investment_cost_chfMW"]["pvrf"][2050]
    expected = annuitise(overnight, amortization_years_all["pvrf"], DISCOUNT_RATE)
    assert expected < overnight
    assert get_cost_component("pvrf", 2050, "investment", scenario) == pytest.approx(expected)
