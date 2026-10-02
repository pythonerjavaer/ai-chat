"""Reusable, input-driven retirement and acquisition decision models."""

from __future__ import annotations

import math
import random
import statistics
import json
import uuid
from datetime import datetime, timezone
from typing import Any


def init_finance_schema(connect):
    with connect() as connection:
        connection.execute("""CREATE TABLE IF NOT EXISTS finance_model_runs (
            id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, model_type TEXT NOT NULL,
            inputs_json TEXT NOT NULL, outputs_json TEXT NOT NULL, created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_finance_runs_user_created ON finance_model_runs(user_id, created_at DESC)")


def save_model_run(connect, user_id: int, model_type: str, inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        connection.execute(
            "INSERT INTO finance_model_runs(id,user_id,model_type,inputs_json,outputs_json,created_at) VALUES(?,?,?,?,?,?)",
            (run_id, user_id, model_type, json.dumps(inputs, ensure_ascii=False), json.dumps(outputs, ensure_ascii=False), created_at),
        )
    return {"id": run_id, "model_type": model_type, "created_at": created_at}


def list_model_runs(connect, user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connect() as connection:
        rows = connection.execute(
            "SELECT id,model_type,inputs_json,outputs_json,created_at FROM finance_model_runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
            (user_id, max(1, min(100, int(limit)))),
        ).fetchall()
    return [{"id": row["id"], "model_type": row["model_type"],
             "inputs": json.loads(row["inputs_json"]), "outputs": json.loads(row["outputs_json"]),
             "created_at": row["created_at"]} for row in rows]


def _validate_rate(name: str, value: float, *, minimum: float = -0.95, maximum: float = 2.0) -> None:
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")


def _optional_amount(payload: dict[str, Any], name: str, *, positive: bool = False) -> float | None:
    raw = payload.get(name)
    if raw in (None, ""):
        return None
    value = float(raw)
    if not math.isfinite(value) or value < 0 or (positive and value == 0):
        raise ValueError(f"{name} must be a finite {'positive' if positive else 'non-negative'} amount.")
    return value


def lifecycle_scenario(payload: dict[str, Any]) -> dict[str, Any]:
    raw_age = float(payload["current_age"])
    raw_retirement_age = float(payload["retirement_age"])
    if not raw_age.is_integer() or not raw_retirement_age.is_integer():
        raise ValueError("Current and retirement ages must be whole years.")
    age, retirement_age = int(raw_age), int(raw_retirement_age)
    if age < 18 or age >= retirement_age or retirement_age > 100:
        raise ValueError("Current age must be 18–99 and retirement age must be greater than current age and no greater than 100.")
    starting_balance = float(payload["starting_balance"])
    salary = float(payload["annual_salary"])
    contribution_rate = float(payload["contribution_rate"])
    salary_growth = float(payload["salary_growth"])
    target_balance = float(payload.get("target_balance", 1_000_000))
    if (not all(map(math.isfinite, (starting_balance, salary, contribution_rate, target_balance)))
            or starting_balance < 0 or salary <= 0 or not 0 <= contribution_rate <= 1 or target_balance < 0):
        raise ValueError("Balance, salary, or contribution rate is outside the supported range.")
    _validate_rate("salary_growth", salary_growth, minimum=-0.2, maximum=0.3)
    raw_simulations = float(payload.get("simulations", 1000))
    if not math.isfinite(raw_simulations) or not raw_simulations.is_integer():
        raise ValueError("simulations must be a whole number between 20 and 20,000.")
    simulations = int(raw_simulations)
    if not 20 <= simulations <= 20_000:
        raise ValueError("simulations must be between 20 and 20,000.")
    seed = int(payload.get("seed", 2023))
    shock_age = payload.get("shock_age")
    if shock_age not in (None, ""):
        raw_shock_age = float(shock_age)
        if not raw_shock_age.is_integer():
            raise ValueError("Shock age must be a whole year.")
        shock_age = int(raw_shock_age)
        if not age <= shock_age < retirement_age:
            raise ValueError("Shock age must fall within the modeled accumulation period.")
    else:
        shock_age = None
    shock_return = float(payload.get("shock_return", -0.37))
    _validate_rate("shock_return", shock_return)
    years = retirement_age - age
    strategies = {
        "balanced": {"name": "Unified balanced", "return": float(payload["balanced_return"]), "volatility": float(payload["balanced_volatility"])},
        "lifecycle": {"name": "Lifecycle staged", "return": float(payload["lifecycle_return"]), "volatility": float(payload["lifecycle_volatility"])},
    }
    for item in strategies.values():
        _validate_rate("expected_return", item["return"], minimum=-0.5, maximum=0.5)
        _validate_rate("volatility", item["volatility"], minimum=0, maximum=1)

    outputs = {}
    for key, strategy in strategies.items():
        terminal: list[float] = []
        drawdowns: list[float] = []
        returns: list[float] = []
        annual_paths = [[] for _ in range(years)]
        for run in range(simulations):
            rng = random.Random(seed + run)
            balance, peak, worst_drawdown = starting_balance, starting_balance, 0.0
            salary_path = salary
            for year in range(years):
                current_age = age + year
                expected = strategy["return"]
                volatility = strategy["volatility"]
                if key == "lifecycle":
                    # Generic de-risking schedule: lower volatility and return
                    # in the final third of the accumulation horizon.
                    progress = max(0.0, (year / max(1, years - 1) - 2 / 3) * 3)
                    expected -= 0.012 * progress
                    volatility *= 1 - 0.35 * progress
                annual_return = shock_return if shock_age == current_age else max(-0.95, rng.gauss(expected, volatility))
                returns.append(annual_return)
                balance = balance * (1 + annual_return) + salary_path * contribution_rate
                salary_path *= 1 + salary_growth
                peak = max(peak, balance)
                worst_drawdown = min(worst_drawdown, balance / peak - 1 if peak else 0)
                annual_paths[year].append(balance)
            terminal.append(balance)
            drawdowns.append(worst_drawdown)
        sorted_terminal = sorted(terminal)
        percentile = lambda probability: sorted_terminal[round(probability * (len(sorted_terminal) - 1))]
        outputs[key] = {
            "name": strategy["name"], "median_terminal_balance": round(statistics.median(terminal), 2),
            "p10_terminal_balance": round(percentile(0.1), 2), "p90_terminal_balance": round(percentile(0.9), 2),
            "mean_terminal_balance": round(statistics.fmean(terminal), 2),
            "annual_return_volatility": round(statistics.pstdev(returns), 5),
            "average_max_drawdown": round(statistics.fmean(drawdowns), 5),
            "target_success_rate": round(sum(value >= target_balance for value in terminal) / simulations, 4),
            "projection": [{"age": age + index + 1, "median_balance": round(statistics.median(values), 2)}
                           for index, values in enumerate(annual_paths)],
        }
        forecasts = {}
        for scenario, return_shift in (("bear", -1.0), ("base", 0.0), ("bull", 1.0)):
            projected, projected_salary = starting_balance, salary
            points = []
            for year in range(years):
                current_age = age + year
                progress = max(0.0, (year / max(1, years - 1) - 2 / 3) * 3)
                expected = strategy["return"] - (0.012 * progress if key == "lifecycle" else 0)
                volatility = strategy["volatility"] * (1 - 0.35 * progress if key == "lifecycle" else 1)
                annual_return = max(-0.95, expected + volatility * return_shift)
                if shock_age == current_age:
                    annual_return = shock_return
                projected = projected * (1 + annual_return) + projected_salary * contribution_rate
                projected_salary *= 1 + salary_growth
                points.append({"age": current_age + 1, "balance": round(projected, 2)})
            forecasts[scenario] = {"terminal_balance": round(projected, 2), "path": points}
        outputs[key]["forecast_scenarios"] = forecasts
    return {"model": "lifecycle Monte Carlo", "simulations": simulations, "seed": seed,
            "member": {"current_age": age, "retirement_age": retirement_age,
                       "starting_balance": starting_balance, "annual_salary": salary,
                       "contribution_rate": contribution_rate, "salary_growth": salary_growth,
                       "target_balance": target_balance},
            "shock": {"age": shock_age, "return": shock_return if shock_age is not None else None},
            "strategies": outputs,
            "assumptions": ["annual independent normal returns, floored at -95%", "year-end contributions", "excludes tax, fees, withdrawals and superannuation rules", "lifecycle strategy linearly de-risks in the final third"]}


def acquisition_scenario(payload: dict[str, Any]) -> dict[str, Any]:
    price = float(payload["purchase_price"])
    debt_share = float(payload["debt_share"])
    debt_rate = float(payload["debt_rate"])
    tax_rate = float(payload["tax_rate"])
    if (not all(map(math.isfinite, (price, debt_share, tax_rate)))
            or price <= 0 or not 0 <= debt_share <= 1 or not 0 <= tax_rate < 1):
        raise ValueError("Purchase price, debt share, or tax rate is outside the supported range.")
    _validate_rate("debt_rate", debt_rate, minimum=0, maximum=1)
    new_debt, cash_consideration = price * debt_share, price * (1 - debt_share)
    incremental_interest = new_debt * debt_rate
    target_ebit = payload.get("target_ebit")
    target_ebit = float(target_ebit) if target_ebit not in (None, "") else None
    if target_ebit is not None and not math.isfinite(target_ebit):
        raise ValueError("target_ebit must be finite.")
    op_cash = payload.get("operating_cash_flow")
    op_cash = float(op_cash) if op_cash not in (None, "") else None
    capex = payload.get("maintenance_capex")
    capex = float(capex) if capex not in (None, "") else None
    if any(value is not None and not math.isfinite(value) for value in (op_cash, capex)):
        raise ValueError("Cash flow and capex must be finite.")
    integration = float(payload.get("integration_cost", 0))
    synergy = float(payload.get("annual_synergy_cash_flow", 0))
    if not math.isfinite(integration) or integration < 0 or not math.isfinite(synergy):
        raise ValueError("Integration cost or synergy cash flow is outside the supported range.")
    free_cash_flow = op_cash - capex + synergy - integration if op_cash is not None and capex is not None else None
    risk_free = float(payload.get("risk_free_rate", 0.04))
    beta = float(payload.get("equity_beta", 1.0))
    premium = float(payload.get("market_risk_premium", 0.05))
    _validate_rate("risk_free_rate", risk_free, minimum=0, maximum=1)
    _validate_rate("market_risk_premium", premium, minimum=0, maximum=1)
    if not math.isfinite(beta) or not 0 <= beta <= 10:
        raise ValueError("equity_beta must be between 0 and 10.")
    existing_debt = _optional_amount(payload, "existing_debt")
    equity = _optional_amount(payload, "equity_value", positive=True)
    acquirer_ebit = payload.get("acquirer_ebit")
    acquirer_ebit = float(acquirer_ebit) if acquirer_ebit not in (None, "") else None
    if acquirer_ebit is not None and not math.isfinite(acquirer_ebit):
        raise ValueError("acquirer_ebit must be finite.")
    existing_interest = _optional_amount(payload, "existing_interest_expense", positive=True)
    acquirer_current_assets = _optional_amount(payload, "acquirer_current_assets")
    acquirer_current_liabilities = _optional_amount(payload, "acquirer_current_liabilities", positive=True)
    target_current_assets = _optional_amount(payload, "target_current_assets")
    target_current_liabilities = _optional_amount(payload, "target_current_liabilities")
    current_debt_portion = _optional_amount(payload, "new_debt_current_portion")
    if current_debt_portion is not None and current_debt_portion > new_debt:
        raise ValueError("new_debt_current_portion cannot exceed new debt.")
    cost_equity = risk_free + beta * premium
    base_capital = existing_debt + equity if existing_debt is not None and equity is not None else None
    post_capital = base_capital + new_debt if base_capital is not None else None
    base_wacc = ((equity / base_capital) * cost_equity + (existing_debt / base_capital) * debt_rate * (1-tax_rate)) if base_capital else None
    post_wacc = ((equity / post_capital) * cost_equity + ((existing_debt + new_debt) / post_capital) * debt_rate * (1-tax_rate)) if post_capital else None
    coverage_before = (acquirer_ebit / existing_interest
                       if acquirer_ebit is not None and existing_interest else None)
    combined_interest = existing_interest + incremental_interest if existing_interest is not None else None
    coverage_after = ((acquirer_ebit + target_ebit) / combined_interest
                      if acquirer_ebit is not None and target_ebit is not None and combined_interest else None)
    current_ratio_before = (acquirer_current_assets / acquirer_current_liabilities
                            if acquirer_current_assets is not None and acquirer_current_liabilities else None)
    post_current_assets = (acquirer_current_assets + target_current_assets - cash_consideration
                           if acquirer_current_assets is not None and target_current_assets is not None else None)
    post_current_liabilities = (acquirer_current_liabilities + target_current_liabilities + current_debt_portion
                                if acquirer_current_liabilities is not None and target_current_liabilities is not None
                                and current_debt_portion is not None else None)
    current_ratio_after = (post_current_assets / post_current_liabilities
                           if post_current_assets is not None and post_current_liabilities else None)
    leverage_before = existing_debt / base_capital if base_capital else None
    leverage_after = (existing_debt + new_debt) / post_capital if post_capital else None
    rate_grid = []
    for shock in (-0.02, -0.01, 0, 0.01, 0.02):
        stressed_rate = max(0, debt_rate + shock)
        stressed_interest = new_debt * stressed_rate
        rate_grid.append({"rate_shock": shock, "debt_rate": stressed_rate,
                          "interest": stressed_interest,
                          "interest_coverage": target_ebit / stressed_interest if target_ebit is not None and stressed_interest else None,
                          "cash_buffer_after_interest": free_cash_flow - stressed_interest if free_cash_flow is not None else None})
    debt_mix_grid = []
    for share in sorted({max(0.0, min(1.0, round(debt_share + shift, 2))) for shift in (-0.2, -0.1, 0, 0.1, 0.2)}):
        scenario_debt = price * share
        scenario_interest = scenario_debt * debt_rate
        scenario_capital = base_capital + scenario_debt if base_capital is not None else None
        scenario_wacc = ((equity / scenario_capital) * cost_equity
                         + ((existing_debt + scenario_debt) / scenario_capital) * debt_rate * (1-tax_rate)) if scenario_capital else None
        debt_mix_grid.append({"debt_share": share, "cash_share": 1-share, "new_debt": round(scenario_debt, 2),
                              "cash_consideration": round(price * (1-share), 2),
                              "annual_interest": round(scenario_interest, 2),
                              "interest_coverage": round(target_ebit / scenario_interest, 3) if target_ebit is not None and scenario_interest else None,
                              "wacc": round(scenario_wacc, 7) if scenario_wacc is not None else None})
    operating_stress = []
    for rate_shock in (-0.02, 0, 0.02):
        stressed_rate = max(0, debt_rate + rate_shock)
        for ebit_shift in (-0.2, -0.1, 0, 0.1, 0.2):
            stressed_ebit = target_ebit * (1 + ebit_shift) if target_ebit is not None else None
            stressed_interest = new_debt * stressed_rate
            operating_stress.append({"rate_shock": rate_shock, "ebit_change": ebit_shift,
                                     "interest_coverage": round(stressed_ebit / stressed_interest, 3)
                                     if stressed_ebit is not None and stressed_interest else None})
    return {"model": "acquisition financing scenario", "currency": str(payload.get("currency", "AUD")),
            "inputs": payload, "new_debt": round(new_debt, 2), "cash_consideration": round(cash_consideration, 2),
            "annual_incremental_interest": round(incremental_interest, 2),
            "interest_coverage": round(target_ebit / incremental_interest, 3) if target_ebit is not None and incremental_interest else None,
            "post_integration_free_cash_flow": round(free_cash_flow, 2) if free_cash_flow is not None else None,
            "cash_buffer_after_interest": round(free_cash_flow - incremental_interest, 2) if free_cash_flow is not None else None,
            "wacc_before": round(base_wacc, 7) if base_wacc is not None else None,
            "wacc_after": round(post_wacc, 7) if post_wacc is not None else None,
            "pro_forma": {
                "interest_coverage_before": round(coverage_before, 3) if coverage_before is not None else None,
                "interest_coverage_after": round(coverage_after, 3) if coverage_after is not None else None,
                "current_ratio_before": round(current_ratio_before, 3) if current_ratio_before is not None else None,
                "current_ratio_after": round(current_ratio_after, 3) if current_ratio_after is not None else None,
                "post_current_assets": round(post_current_assets, 2) if post_current_assets is not None else None,
                "post_current_liabilities": round(post_current_liabilities, 2) if post_current_liabilities is not None else None,
                "debt_to_capital_before": round(leverage_before, 5) if leverage_before is not None else None,
                "debt_to_capital_after": round(leverage_after, 5) if leverage_after is not None else None,
            },
            "rate_sensitivity": rate_grid,
            "financing_mix_sensitivity": debt_mix_grid,
            "operating_stress_matrix": operating_stress,
            "missing_inputs": [name for name, value in (("target_ebit", target_ebit), ("operating_cash_flow", op_cash), ("maintenance_capex", capex)) if value is None],
            "assumptions": ["simplified pro-forma comparison, not audited consolidated statements", "cash consideration reduces combined current assets; current debt portion increases current liabilities", "synergies are included only when user supplied", "tax and WACC use user-provided assumptions"]}
