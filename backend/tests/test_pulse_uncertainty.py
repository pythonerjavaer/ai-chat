import numpy as np
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient

from backend.product_domains.pulse_uncertainty import deposit_uncertainty
from backend.product_domains.pulse import PulseRepository, CustomerWrite, SKUWrite, OrderWrite, OrderLineWrite, ExpenseWrite
from backend.tests.test_product_domains import product_store  # noqa: F401
from backend.tests.test_postgres_application import persistent_app, register  # noqa: F401


def rows(customers=24, orders=2):
    return [{"customer_id": f"c{customer}", "order_id": f"o{customer}-{order}",
             "amount_cents": 2000 + customer * 900 + order * 1300}
            for customer in range(customers) for order in range(orders)]


def test_bootstrap_is_reproducible_bounded_and_evaluates_observed_severity():
    result = deposit_uncertainty(rows())
    assert result == deposit_uncertainty(rows())
    assert result["status"] == "ok"
    assert result["customers"] == 24 and result["loss_orders"] == 48
    assert result["resamples"] == 2000
    for scenario in result["scenarios"]:
        low, high = scenario["coverage_ratio_ci95"]
        assert 0 <= low < high <= 1
        low, high = scenario["mean_uncovered_per_loss_order_cents_ci95"]
        assert 0 <= low < high
    assert result["scenarios"][1]["coverage_ratio_ci95"][0] > result["scenarios"][0]["coverage_ratio_ci95"][0]
    assert "不是损失发生概率" in result["interpretation"]


def test_repeat_customers_are_not_treated_as_independent_orders():
    result = deposit_uncertainty(rows(customers=2, orders=100))
    assert result["status"] == "insufficient_data"
    assert result["customers"] == 2 and not result["scenarios"]


def test_customer_cluster_ratio_matches_direct_reference_with_variable_group_sizes():
    data = rows()
    data += [{"customer_id": "c0", "order_id": "extra", "amount_cents": 85000}]
    result = deposit_uncertainty(data)
    groups = {}
    for row in data:
        groups.setdefault(row["customer_id"], []).append(row["amount_cents"])
    rng = np.random.default_rng(2026)
    reference = []
    for _ in range(2000):
        picked = rng.integers(0, len(groups), size=len(groups))
        sample = [value for index in picked for value in list(groups.values())[index]]
        reference.append(sum(min(value, 5000) for value in sample) / sum(sample))
    expected = [round(float(value), 4) for value in np.quantile(reference, [0.025, 0.975])]
    assert result["scenarios"][0]["coverage_ratio_ci95"] == expected


def test_empty_small_constant_invalid_and_truncated_data_never_get_fake_intervals():
    assert deposit_uncertainty([])["status"] == "insufficient_data"
    assert deposit_uncertainty(rows(20, 1))["status"] == "insufficient_data"
    constant = [{**row, "amount_cents": 5000} for row in rows()]
    assert deposit_uncertainty(constant)["status"] == "no_variation"
    for bad in ({"customer_id": None}, {"order_id": None}, {"amount_cents": -1}, {"amount_cents": True}):
        data = rows()
        data[0].update(bad)
        assert deposit_uncertainty(data)["status"] == "invalid_source"
    assert deposit_uncertainty(rows() + [rows()[0]])["status"] == "invalid_source"
    assert deposit_uncertainty(rows(), truncated=True)["status"] == "data_limit"
    assert deposit_uncertainty(rows(2001))["status"] == "data_limit"


def test_actual_analytics_uses_owned_order_losses_and_date_scope(product_store):
    repo = PulseRepository(product_store)
    sku = repo.create_sku(1, SKUWrite(name="损失测试商品", current_price_cents=20000))
    start = datetime.now(timezone.utc) + timedelta(days=2)
    for customer_index in range(20):
        customer = repo.create_customer(1, CustomerWrite(name=f"隔离测试客户{customer_index}"))
        for order_index in range(2):
            order = repo.create_order(1, OrderWrite(customer_id=customer["id"], start_at=start,
                end_at=start + timedelta(days=1), items=[OrderLineWrite(sku_id=sku["id"]) ]))
            # Multiple expense lines in one order must remain ONE loss event.
            for amount in (2000 + 500 * customer_index, 1000 + order_index * 100):
                repo.record_expense(1, ExpenseWrite(category="repair", order_id=order["id"], amount_cents=amount))
    result = repo.analytics(1, "2000-01-01", "2099-12-31")["deposit_coverage"]
    assert result["synthetic"] is False
    assert result["uncertainty"]["status"] == "ok"
    assert result["uncertainty"]["loss_orders"] == 40 and result["uncertainty"]["customers"] == 20
    assert result["scenarios"][0]["observations"] == 40
    for user, date_from, date_to in ((2, "2000-01-01", "2099-12-31"), (1, "2000-01-01", "2001-01-01")):
        repo.ensure_user_setup(user)
        other = repo.analytics(user, date_from, date_to)["deposit_coverage"]["uncertainty"]
        assert other["loss_orders"] == 0 and other["scenarios"] == []


def test_postgres_aggregate_money_is_normalized_before_bootstrap(persistent_app):
    with TestClient(persistent_app.main.app) as client:
        headers, _ = register(client, "bootstrap-pg-one")
        register(client, "bootstrap-pg-two")
        test_actual_analytics_uses_owned_order_losses_and_date_scope(persistent_app.database.connect)
        response = client.get("/api/pulse/analytics?date_from=2000-01-01&date_to=2099-12-31", headers=headers)
        assert response.status_code == 200
        result = response.json()["deposit_coverage"]["uncertainty"]
        assert result["status"] == "ok" and result["customers"] == 20 and result["loss_orders"] == 40
