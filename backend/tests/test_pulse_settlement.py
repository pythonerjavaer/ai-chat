"""Exercise real rental records and ledgers, not a separate settlement demo."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend.product_domains.pulse import (
    PulseRepository, PaymentWrite, ExpenseWrite, OrderWrite, OrderLineWrite,
    OrderStatusWrite, SettingsWrite, InspectionWrite, create_pulse_router,
)
from backend.product_domains.pulse_settlement import (
    SettlementRepository, SettlementConflict, BillWrite, ClearingWrite, VendorWrite, ReversalWrite,
)
from backend.tests.test_product_domains import product_store, _pulse_master_data  # noqa: F401
from backend.tests.test_postgres_application import persistent_app, register  # noqa: F401


def key():
    return str(uuid.uuid4())


def setup_order(connect):
    repo = PulseRepository(connect)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    order = repo.create_order(1, OrderWrite(customer_id=customer["id"], start_at=start,
        end_at=start + timedelta(days=2), items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])]))
    return repo, SettlementRepository(repo), order, asset


def bill_request(source):
    return BillWrite(request_id=key(), source_id=source, due_date="2026-01-01")


def amounts(repo):
    return repo.statements(1, "2000-01-01", "2099-12-31")


def test_wrong_receipt_can_be_auditably_reversed_and_reentered(product_store):
    repo, service, order, asset = setup_order(product_store)
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    bill = service.open_bill(1, "receivable", bill_request(order["id"]))["bill"]
    first = service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=2000))
    entry = service.bill_detail(1, bill["id"])["allocations"][0]
    command = ReversalWrite(request_id=key(), reason="实际收款金额录错")
    result = service.reverse_allocation(1, entry["id"], command)
    assert result == service.reverse_allocation(1, entry["id"], command)
    assert result["bill"]["remaining_cents"] == 10000
    assert result["journal"]["reversal_of"] == first["journal"]["id"]
    assert service.quote(1, order["id"])["rental_due_cents"] == 10000
    assert amounts(repo)["balance_sheet"]["assets"]["Accounts Receivable"] == 10000
    assert amounts(repo)["cash_flow"]["operating"] == 0
    assert repo.asset_economics(1, asset["id"])["lifetime_revenue"] == 0
    assert repo.analytics(1, "2000-01-01", "2099-12-31")["eda"]["monthly_trends"] == []
    assert repo.dashboard(1, "2000-01-01", "2099-12-31")["metrics"]["outstanding_receivables"] == 10000
    assert repo.dashboard(1, "2000-01-01", "2099-12-31")["metrics"]["cash_in"] == 0
    assert repo.dashboard(1, "2000-01-01", "2099-12-31")["metrics"]["cash_out"] == 0
    assert service.bill_detail(1, bill["id"])["allocations"][0]["reversal_reason"] == command.reason
    with pytest.raises(SettlementConflict, match="已经冲销"):
        service.reverse_allocation(1, entry["id"], ReversalWrite(request_id=key(), reason="重复进行冲销"))
    with pytest.raises(KeyError):
        service.reverse_allocation(2, entry["id"], command)
    service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=1800))
    assert service.quote(1, order["id"])["rental_due_cents"] == 8200
    assert repo.trial_balance(1, "2000-01-01", "2099-12-31")["balanced"]


def test_deposit_offset_reversal_restores_deposit_without_cashflow(product_store):
    repo, service, order, _ = setup_order(product_store)
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit", amount_cents=3000))
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    repo.transition_order(1, order["id"], OrderStatusWrite(status="returned"))
    bill = service.open_bill(1, "receivable", bill_request(order["id"]))["bill"]
    service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=2000), deposit=True)
    entry = service.bill_detail(1, bill["id"])["allocations"][0]
    service.reverse_allocation(1, entry["id"], ReversalWrite(request_id=key(), reason="押金抵扣金额录错"))
    assert service.quote(1, order["id"])["deposit_available_cents"] == 3000
    metrics = repo.dashboard(1, "2000-01-01", "2099-12-31")["metrics"]
    assert metrics["deposits_held"] == 3000 and metrics["cash_in"] == 3000 and metrics["cash_out"] == 0


def test_supplier_reversal_restores_payable_and_closed_period_rolls_back(product_store):
    repo, service, _, _ = setup_order(product_store)
    expense = repo.record_expense(1, ExpenseWrite(category="laundry", amount_cents=6000, status="unpaid"))["expense"]
    bill = service.open_bill(1, "payable", bill_request(expense["id"]))["bill"]
    service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=2000))
    entry = service.bill_detail(1, bill["id"])["allocations"][0]
    period = datetime.now(timezone.utc).strftime("%Y-%m")
    with product_store() as connection:
        connection.execute("INSERT INTO pulse_accounting_periods(id,user_id,period_key,status,created_at) VALUES(?,1,?,'CLOSED','fixture')", (key(), period))
    command = ReversalWrite(request_id=key(), reason="付款科目录入错误")
    with pytest.raises(ValueError, match="关闭|closed|结账",):
        service.reverse_allocation(1, entry["id"], command)
    assert service.bill_detail(1, bill["id"])["bill"]["settled_cents"] == 2000
    with product_store() as connection:
        assert connection.execute("SELECT COUNT(*) FROM pulse_settlement_reversals").fetchone()[0] == 0
        connection.execute("UPDATE pulse_accounting_periods SET status='OPEN' WHERE user_id=1")
    service.reverse_allocation(1, entry["id"], command)
    assert repo.list_entity(1, "pulse_expenses")[0]["status"] == "unpaid"
    assert amounts(repo)["balance_sheet"]["liabilities"]["Accounts Payable"] == 6000
    assert amounts(repo)["cash_flow"]["operating"] == 0


def test_receivable_remaining_only_and_cash_clears_without_double_revenue(product_store):
    repo, service, order, _ = setup_order(product_store)
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="rental", amount_cents=3000))
    with pytest.raises(ValueError, match="已交付"):
        service.open_bill(1, "receivable", bill_request(order["id"]))
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    command = bill_request(order["id"])
    bill = service.open_bill(1, "receivable", command)["bill"]
    assert bill["amount_cents"] == 7000 and bill["overdue"]
    assert service.open_bill(1, "receivable", command)["bill"]["id"] == bill["id"]
    assert service.open_bill(1, "receivable", bill_request(order["id"]))["bill"]["id"] == bill["id"]
    assert amounts(repo)["income_statement"]["revenue_total"] == 10000
    assert amounts(repo)["balance_sheet"]["assets"]["Accounts Receivable"] == 7000
    clear = ClearingWrite(request_id=key(), amount_cents=2000, cash_account_code="1010")
    cleared = service.clear_bill(1, bill["id"], clear)
    assert cleared["bill"]["remaining_cents"] == 5000
    assert service.clear_bill(1, bill["id"], clear) == cleared
    assert amounts(repo)["income_statement"]["revenue_total"] == 10000
    assert amounts(repo)["balance_sheet"]["assets"]["Accounts Receivable"] == 5000
    assert service.quote(1, order["id"])["rental_due_cents"] == 5000
    # Existing transaction desk also clears the bill instead of recognizing income again.
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="rental", amount_cents=5000))
    assert service.bill_detail(1, bill["id"])["bill"]["status"] == "settled"
    assert amounts(repo)["income_statement"]["revenue_total"] == 10000
    assert repo.trial_balance(1, "2000-01-01", "2099-12-31")["balanced"]
    with pytest.raises(SettlementConflict):
        repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="rental", amount_cents=1))


def test_deposit_offset_refund_damage_evidence_and_no_fictitious_cash(product_store):
    repo, service, order, asset = setup_order(product_store)
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit", amount_cents=5000))
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    bill = service.open_bill(1, "receivable", bill_request(order["id"]))["bill"]
    with pytest.raises(ValueError, match="归还"):
        service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=1000), deposit=True)
    repo.transition_order(1, order["id"], OrderStatusWrite(status="returned"))
    before_cash = amounts(repo)["cash_flow"]["operating"]
    service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=2000), deposit=True)
    quote = service.quote(1, order["id"])
    assert quote["deposit_available_cents"] == 3000 and quote["rental_due_cents"] == 8000
    assert amounts(repo)["cash_flow"]["operating"] == before_cash
    metrics = repo.dashboard(1, "2000-01-01", "2099-12-31")["metrics"]
    assert metrics["deposits_held"] == 3000
    assert metrics["cash_in"] == 5000 and metrics["cash_out"] == 0
    with pytest.raises(ValueError, match="损坏"):
        repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="damage_deduction", amount_cents=500))
    repo.create_inspection(1, InspectionWrite(order_id=order["id"], asset_id=asset["id"],
        condition_status="damaged", resolution_status="confirmed", damage_notes="Confirmed repair charge"))
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="damage_deduction", amount_cents=500))
    assert amounts(repo)["cash_flow"]["operating"] == before_cash
    refund = PaymentWrite(request_id=key(), order_id=order["id"], payment_type="deposit_refund", amount_cents=2500)
    assert repo.record_payment(1, refund) == repo.record_payment(1, refund)
    assert service.quote(1, order["id"])["deposit_available_cents"] == 0
    with pytest.raises(SettlementConflict):
        repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit_refund", amount_cents=1))


def test_payable_part_payments_do_not_recognize_expense_twice(product_store):
    repo, service, _, _ = setup_order(product_store)
    vendor = service.create_vendor(1, VendorWrite(request_id=key(), name="Test laundry"))
    expense = repo.record_expense(1, ExpenseWrite(category="laundry", amount_cents=6000, status="unpaid",
        vendor_id=vendor["id"], description="Supplier invoice A"))["expense"]
    bill = service.open_bill(1, "payable", bill_request(expense["id"]))["bill"]
    assert bill["counterparty"] == "Test laundry"
    assert amounts(repo)["balance_sheet"]["liabilities"]["Accounts Payable"] == 6000
    for amount, expected_status in [(2000, "partial"), (4000, "settled")]:
        result = service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=amount))
        assert result["bill"]["status"] == expected_status
        assert sum(amounts(repo)["income_statement"]["expenses"].values()) == 6000
        assert repo.dashboard(1, "2000-01-01", "2099-12-31")["metrics"]["cash_out"] == (2000 if amount == 2000 else 6000)
    assert amounts(repo)["balance_sheet"]["liabilities"]["Accounts Payable"] == 0
    assert repo.list_entity(1, "pulse_expenses")[0]["status"] == "paid"
    assert amounts(repo)["cash_flow"]["operating"] == -6000
    assert len(service.bill_detail(1, bill["id"])["allocations"]) == 2
    with pytest.raises(SettlementConflict):
        service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=1))
    with pytest.raises(ValueError, match="客户押金"):
        service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=1), deposit=True)


def test_isolation_atomic_rollback_currency_and_conflicting_replay(product_store):
    repo, service, order, _ = setup_order(product_store)
    command = PaymentWrite(request_id=key(), order_id=order["id"], payment_type="deposit", amount_cents=1000)
    first = repo.record_payment(1, command)
    assert repo.record_payment(1, command) == first
    with pytest.raises(SettlementConflict):
        repo.record_payment(1, command.model_copy(update={"amount_cents": 2000}))
    with pytest.raises(KeyError):
        service.quote(2, order["id"])
    with pytest.raises(ValueError, match="币种"):
        repo.update_settings(1, SettingsWrite(currency="USD"))
    def counts():
        with product_store() as connection:
            return [connection.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=1").fetchone()["n"]
                    for table in ("pulse_payments", "pulse_expenses", "pulse_journals")]
    before = counts()
    # Closed period fails after source INSERT; the transaction must roll it all back.
    with product_store() as connection:
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        connection.execute("INSERT INTO pulse_accounting_periods(id,user_id,period_key,status,created_at) VALUES(?,1,?,'CLOSED','fixture')", (key(), period))
    with pytest.raises(ValueError, match="已关闭"):
        repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit", amount_cents=500))
    with pytest.raises(ValueError, match="已关闭"):
        repo.record_expense(1, ExpenseWrite(category="laundry", amount_cents=500))
    assert counts() == before
    with pytest.raises(ValueError, match="部分付款"):
        repo.record_expense(1, ExpenseWrite(category="laundry", amount_cents=500, status="partially_paid"))


def test_concurrent_clear_cannot_overdraw_bill_or_duplicate_commands(product_store):
    repo, service, order, _ = setup_order(product_store)
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    bill = service.open_bill(1, "receivable", bill_request(order["id"]))["bill"]
    command = ClearingWrite(request_id=key(), amount_cents=6000)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.clear_bill(1, bill["id"], command), range(2)))
    assert results[0] == results[1]
    assert service.bill_detail(1, bill["id"])["bill"]["settled_cents"] == 6000
    def attempt(_):
        try:
            service.clear_bill(1, bill["id"], ClearingWrite(request_id=key(), amount_cents=3000))
            return "ok"
        except SettlementConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(attempt, range(2))) == ["conflict", "ok"]
    assert service.bill_detail(1, bill["id"])["bill"]["remaining_cents"] == 1000


def test_strict_amounts_api_scopes_pagination_and_no_bank_feature(product_store):
    with pytest.raises(ValidationError):
        ClearingWrite(request_id=key(), amount_cents=1.5)
    with pytest.raises(ValidationError):
        ClearingWrite(request_id=key(), amount_cents=True)
    with pytest.raises(ValidationError):
        ClearingWrite(request_id=key(), amount_cents=1, occurred_at="2026-10-21T12:00:00")
    repo, service, order, _ = setup_order(product_store)
    app = FastAPI()
    current = {"id": 1}
    app.include_router(create_pulse_router(product_store, lambda: current))
    with TestClient(app) as client:
        assert client.get("/api/pulse/settlements/overview?page_size=101").status_code == 422
        assert client.get("/api/pulse/settlements/overview").json()["bills"] == []
        assert "bank_reconciliation" in client.get("/api/pulse/capabilities").json()["planned"]
        assert client.post("/api/pulse/settlements/bank-imports", json={}).status_code == 404
        body = {"request_id": key(), "order_id": order["id"], "payment_type": "deposit", "amount_cents": 3000}
        assert client.post("/api/pulse/payments", json=body).status_code == 201
        body["amount_cents"] = 4000
        assert client.post("/api/pulse/payments", json=body).status_code == 409
        current["id"] = 2
        assert client.get(f"/api/pulse/settlements/orders/{order['id']}").status_code == 404


def test_settlement_postgres_http_and_actual_ledger(persistent_app):
    with TestClient(persistent_app.main.app) as client:
        assert client.get("/api/pulse/settlements/overview").status_code == 401
        headers, _ = register(client, "settlement-pg")
        expense = client.post("/api/pulse/expenses", headers=headers, json={
            "category": "laundry", "amount_cents": 3000, "status": "unpaid", "request_id": key(),
        })
        assert expense.status_code == 201, expense.text
        opened = client.post("/api/pulse/settlements/bills/payable", headers=headers,
                             json=bill_request(expense.json()["expense"]["id"]).model_dump(mode="json"))
        assert opened.status_code == 201, opened.text
        bill_id = opened.json()["bill"]["id"]
        body = {"request_id": key(), "amount_cents": 2000}
        paid = client.post(f"/api/pulse/settlements/bills/{bill_id}/clear", headers=headers, json=body)
        assert paid.status_code == 200, paid.text
        assert client.post(f"/api/pulse/settlements/bills/{bill_id}/clear", headers=headers, json=body).json() == paid.json()
        assert paid.json()["bill"]["remaining_cents"] == 1000
        statement = client.get("/api/pulse/statements?date_from=2000-01-01&date_to=2099-12-31", headers=headers).json()
        assert statement["balance_sheet"]["liabilities"]["Accounts Payable"] == 1000
        assert statement["cash_flow"]["operating"] == -2000
        def racing_payment(_):
            return client.post(f"/api/pulse/settlements/bills/{bill_id}/clear", headers=headers,
                               json={"request_id": key(), "amount_cents": 700}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(racing_payment, range(2))) == [200, 409]
        other_headers, _ = register(client, "other-settlement-pg")
        assert client.get(f"/api/pulse/settlements/bills/{bill_id}", headers=other_headers).status_code == 404
        detail = client.get(f"/api/pulse/settlements/bills/{bill_id}", headers=headers).json()
        entry = next(row for row in detail["allocations"] if row["amount_cents"] == 2000)
        reverse_path = f"/api/pulse/settlements/allocations/{entry['id']}/reverse"
        reverse_body = {"request_id": key(), "reason": "实际付款金额录错"}
        assert client.post(reverse_path, headers=other_headers, json=reverse_body).status_code == 404
        reversed_entry = client.post(reverse_path, headers=headers, json=reverse_body)
        assert reversed_entry.status_code == 200, reversed_entry.text
        assert reversed_entry.json()["bill"]["remaining_cents"] == 2300
        assert client.post(reverse_path, headers=headers, json=reverse_body).json() == reversed_entry.json()
        def racing_reversal(_):
            remaining = next(row for row in detail["allocations"] if row["amount_cents"] == 700)
            return client.post(f"/api/pulse/settlements/allocations/{remaining['id']}/reverse", headers=headers,
                               json={"request_id": key(), "reason": "测试并发更正记录"}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(racing_reversal, range(2))) == [200, 409]
        assert client.get(f"/api/pulse/settlements/bills/{bill_id}", headers=headers).json()["bill"]["remaining_cents"] == 3000
        statement = client.get("/api/pulse/statements?date_from=2000-01-01&date_to=2099-12-31", headers=headers).json()
        assert statement["balance_sheet"]["liabilities"]["Accounts Payable"] == 3000
        assert statement["cash_flow"]["operating"] == 0
