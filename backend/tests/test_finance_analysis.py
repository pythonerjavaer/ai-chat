import sqlite3

import pytest

from backend.finance_analysis import (
    acquisition_scenario,
    init_finance_schema,
    lifecycle_scenario,
    list_model_runs,
    save_model_run,
)


def test_lifecycle_scenario_is_reproducible_and_returns_projection():
    inputs = {
        "current_age": 35, "retirement_age": 67, "starting_balance": 50_000,
        "annual_salary": 90_000, "contribution_rate": .12, "salary_growth": .03,
        "balanced_return": .084, "balanced_volatility": .091,
        "lifecycle_return": .078, "lifecycle_volatility": .081,
        "simulations": 100, "seed": 2023, "shock_age": 59, "shock_return": -.37,
    }
    first = lifecycle_scenario(inputs)
    second = lifecycle_scenario(inputs)
    assert first == second
    assert first["simulations"] == 100
    assert len(first["strategies"]["balanced"]["projection"]) == 32
    assert first["strategies"]["lifecycle"]["average_max_drawdown"] <= 0
    interval = first["strategies"]["balanced"]["terminal_balance_bootstrap"]
    assert interval["status"] == "ok"
    assert interval["lower_95"] <= interval["mean"] <= interval["upper_95"]


def test_lifecycle_rejects_invalid_horizon():
    with pytest.raises(ValueError, match="age"):
        lifecycle_scenario({"current_age": 67, "retirement_age": 67})


@pytest.mark.parametrize("current_age,retirement_age", [(10_000, 10_001), (35.5, 67), (35, 35.5)])
def test_lifecycle_rejects_impossible_or_fractional_ages(current_age, retirement_age):
    with pytest.raises(ValueError, match="age|Age"):
        lifecycle_scenario({"current_age": current_age, "retirement_age": retirement_age})


def test_lifecycle_rejects_market_shock_outside_projection_period():
    with pytest.raises(ValueError, match="Shock age"):
        lifecycle_scenario({
            "current_age": 35, "retirement_age": 67, "starting_balance": 50_000,
            "annual_salary": 90_000, "contribution_rate": .12, "salary_growth": .03,
            "balanced_return": .084, "balanced_volatility": .091,
            "lifecycle_return": .078, "lifecycle_volatility": .081,
            "shock_age": 99,
        })


def test_acquisition_model_keeps_missing_inputs_explicit_and_builds_rate_sensitivity():
    result = acquisition_scenario({
        "purchase_price": 160, "debt_share": .7, "debt_rate": .0843,
        "tax_rate": .3, "target_ebit": None, "operating_cash_flow": None,
        "maintenance_capex": None, "integration_cost": 0,
        "annual_synergy_cash_flow": 0,
    })
    assert result["new_debt"] == 112
    assert result["cash_consideration"] == 48
    assert result["annual_incremental_interest"] == pytest.approx(9.44)
    assert result["interest_coverage"] is None
    assert result["missing_inputs"] == ["target_ebit", "operating_cash_flow", "maintenance_capex"]
    assert len(result["rate_sensitivity"]) == 5
    assert result["wacc_before"] is None  # Capital structure was not supplied.
    assert result["pro_forma"]["current_ratio_after"] is None
    assert result["scenario_distribution"]["status"] == "missing_operating_inputs"


def test_acquisition_compares_pre_and_post_financial_ratios_from_manual_inputs():
    result = acquisition_scenario({
        "purchase_price": 100, "debt_share": .6, "debt_rate": .1, "tax_rate": .3,
        "target_ebit": 20, "existing_debt": 200, "equity_value": 400,
        "acquirer_ebit": 80, "existing_interest_expense": 10,
        "acquirer_current_assets": 150, "acquirer_current_liabilities": 100,
        "target_current_assets": 50, "target_current_liabilities": 20,
        "new_debt_current_portion": 10,
    })
    comparison = result["pro_forma"]
    assert comparison["interest_coverage_before"] == 8
    assert comparison["interest_coverage_after"] == 6.25
    assert comparison["current_ratio_before"] == 1.5
    assert comparison["post_current_assets"] == 160
    assert comparison["post_current_liabilities"] == 130
    assert comparison["current_ratio_after"] == 1.231
    assert comparison["debt_to_capital_after"] > comparison["debt_to_capital_before"]
    assert len({row["wacc"] for row in result["financing_mix_sensitivity"]}) > 1
    assert result["scenario_distribution"]["status"] == "available"


def test_acquisition_rejects_unreasonable_new_current_debt():
    with pytest.raises(ValueError, match="new_debt_current_portion"):
        acquisition_scenario({"purchase_price": 100, "debt_share": .2, "debt_rate": .1,
                              "tax_rate": .3, "new_debt_current_portion": 21})


def test_model_runs_are_persisted_per_user(tmp_path):
    path = tmp_path / "finance.db"

    def connect():
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    with connect() as connection:
        connection.execute("CREATE TABLE users(id INTEGER PRIMARY KEY)")
        connection.executemany("INSERT INTO users(id) VALUES(?)", [(1,), (2,)])
    init_finance_schema(connect)
    saved = save_model_run(connect, 1, "acquisition", {"price": 42}, {"debt": 30})
    assert saved["id"]
    assert list_model_runs(connect, 1)[0]["outputs"] == {"debt": 30}
    assert list_model_runs(connect, 2) == []
