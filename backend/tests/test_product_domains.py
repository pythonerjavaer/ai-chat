from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from backend.product_domains import init_leap_schema, init_pulse_schema
from backend.tests.test_postgres_application import persistent_app, register  # noqa: F401
from backend.product_domains.leap import (
    ClashWrite,
    ExcerptCreate,
    LeapRepository,
    MaterialCreate,
    NoteWrite,
    ProgressUpdate,
    WormholeWrite,
    DemoAction,
)
from backend.product_domains.pulse import (
    AssetStatusWrite,
    AssetWrite,
    CustomerWrite,
    InspectionWrite,
    OrderLineWrite,
    OrderWrite,
    PaymentWrite,
    PulseRepository,
    SKUWrite,
    ExpenseWrite,
    OrderStatusWrite,
    PulseDemoAction,
)


@pytest.fixture()
def product_store(tmp_path):
    path = tmp_path / "domains.db"

    def connect():
        connection = sqlite3.connect(path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    with connect() as connection:
        connection.executescript(
            "CREATE TABLE users(id INTEGER PRIMARY KEY, username TEXT);"
            "INSERT INTO users VALUES(1,'one'); INSERT INTO users VALUES(2,'two');"
        )
    init_leap_schema(connect)
    init_pulse_schema(connect)
    return connect


def test_leap_evidence_wormhole_clash_progress_and_isolation(product_store):
    repo = LeapRepository(product_store)
    left = repo.create_material(1, MaterialCreate(title="历史", author="A", text="第一段证据。\n\n第二段。"))
    right = repo.create_material(1, MaterialCreate(title="哲学", author="B", text="另一条证据。\n\n结论。"))
    repo.update_progress(1, left["id"], ProgressUpdate(paragraph_position=1))
    first = repo.create_excerpt(1, ExcerptCreate(
        material_id=left["id"], material_version=1, paragraph_position=0, quote="第一段证据。",
    ))
    second = repo.create_excerpt(1, ExcerptCreate(
        material_id=right["id"], material_version=1, paragraph_position=0, quote="另一条证据。",
    ))
    note = repo.write_note(1, NoteWrite(material_id=left["id"], excerpt_id=first["id"], content="我的解释"))
    wormhole = repo.write_wormhole(1, WormholeWrite(
        left_excerpt_id=first["id"], right_excerpt_id=second["id"],
        relation_type="对立", reflection="两条证据的条件不同。",
    ))
    clash = repo.write_clash(1, ClashWrite(
        title="双观点", viewpoint_a="观点A", viewpoint_b="观点B",
        evidence_a_excerpt_id=first["id"], evidence_b_excerpt_id=second["id"],
        disagreement="适用范围", judgment="保留条件判断",
    ))

    assert repo.get_material(1, left["id"])["progress_percent"] == 100
    assert repo.get_excerpt(1, first["id"])["quote"] == "第一段证据。"
    assert repo.list_notes(1, "")[0]["id"] == note["id"]
    assert repo.get_wormhole(1, wormhole["id"])["right_quote"] == "另一条证据。"
    assert repo.list_clashes(1)[0]["id"] == clash["id"]
    with pytest.raises(KeyError):
        repo.get_material(2, left["id"])
    assert repo.list_excerpts(2) == []


def _pulse_master_data(repo: PulseRepository, user_id: int = 1):
    customer = repo.create_customer(user_id, CustomerWrite(name="Customer A"))
    sku = repo.create_sku(user_id, SKUWrite(name="Evening Dress", current_price_cents=10_000))
    asset = repo.create_asset(user_id, AssetWrite(
        sku_id=sku["id"], asset_code="OIA-001", accounting_class="rental_asset",
        purchase_cost_cents=50_000,
    ))
    return customer, sku, asset


def test_pulse_price_snapshot_deposit_accounting_and_asset_lifecycle(product_store):
    repo = PulseRepository(product_store)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    order = repo.create_order(1, OrderWrite(
        customer_id=customer["id"], start_at=start, end_at=start + timedelta(days=2),
        items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])],
    ))
    with product_store() as connection:
        connection.execute("UPDATE pulse_skus SET current_price_cents=25000 WHERE id=?", (sku["id"],))
    assert repo.order_detail(1, order["id"])["items"][0]["unit_price_cents"] == 10_000

    deposit = repo.record_payment(1, PaymentWrite(
        order_id=order["id"], payment_type="deposit", amount_cents=3_000,
    ))
    rental = repo.record_payment(1, PaymentWrite(
        order_id=order["id"], payment_type="rental", amount_cents=10_000,
    ))
    assert deposit["journal"]["balanced"] is True
    assert rental["journal"]["balanced"] is True
    statements = repo.statements(1, "2020-01-01", "2035-12-31")
    assert statements["income_statement"]["revenue_total"] == 10_000
    assert repo.dashboard(1, "2020-01-01", "2035-12-31")["metrics"]["deposits_held"] == 3_000
    expense = repo.record_expense(1, ExpenseWrite(
        category="cleaning", amount_cents=800, asset_id=asset["id"], description="Return cleaning",
    ))
    assert expense["expense"]["id"]
    assert expense["journal"]["balanced"] is True
    assert repo.dashboard(1, "2020-01-01", "2035-12-31")["metrics"]["cash_out"] == 800

    repo.transition_asset(1, asset["id"], AssetStatusWrite(status="rented", order_id=order["id"]))
    repo.transition_asset(1, asset["id"], AssetStatusWrite(status="inspection", order_id=order["id"]))
    repo.create_inspection(1, InspectionWrite(
        order_id=order["id"], asset_id=asset["id"],
        condition_status="cleaning_required", resolution_status="confirmed",
    ))
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "cleaning"
    assert repo.trial_balance(1, "2020-01-01", "2035-12-31")["balanced"] is True


def test_pulse_overlap_is_rejected_even_for_concurrent_attempts(product_store):
    repo = PulseRepository(product_store)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=3)
    payload = OrderWrite(
        customer_id=customer["id"], start_at=start, end_at=start + timedelta(days=1),
        items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])],
    )

    def create():
        try:
            return repo.create_order(1, payload)["id"]
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sum(value is not None for value in results) == 1
    assert len(repo.list_entity(1, "pulse_orders")) == 1


def test_pulse_user_data_isolation(product_store):
    repo = PulseRepository(product_store)
    customer, _, _ = _pulse_master_data(repo, 1)
    assert repo.list_entity(2, "pulse_customers") == []
    with product_store() as connection:
        with pytest.raises(KeyError):
            repo._owned(connection, "pulse_customers", customer["id"], 2)


def test_leap_demo_is_isolated_persistent_and_evidence_backed(product_store):
    repo = LeapRepository(product_store)
    demo = repo.reset_demo(1)
    assert demo["loaded"] is True
    assert len(demo["materials"]) == 4
    assert repo.list_materials(1, "", 30, 0)["total"] == 0
    material = demo["materials"][0]
    updated = repo.demo_action(1, DemoAction(action="excerpt", payload={
        "material_id": material["id"], "paragraph_position": 0,
        "quote": material["paragraphs"][0], "start_offset": 0,
    }))
    assert len(updated["excerpts"]) == len(demo["excerpts"]) + 1
    assert repo.demo(2)["loaded"] is False
    assert any(node["kind"] == "theme" for node in updated["universe"]["nodes"])


def test_pulse_demo_full_transaction_updates_balanced_finance(product_store):
    repo = PulseRepository(product_store)
    demo = repo.reset_demo(1)
    assert demo["trial_balance"]["balanced"] is True
    assert demo["statements"]["balance_sheet"]["balanced"] is True
    assert demo["analytics"]["customer_summary"]["cohorts"]
    assert demo["analytics"]["customer_summary"]["channel_revenue"]
    assert demo["analytics"]["top_assets"][0]["revenue_per_available_day"] > 0
    assert repo.list_entity(1, "pulse_orders") == []
    customer = repo.demo_action(1, PulseDemoAction(action="customer", payload={"name": "Journey Customer"}))["customers"][-1]
    available = next(asset for asset in repo.demo(1)["assets"] if asset["status"] == "available")
    order = repo.demo_action(1, PulseDemoAction(action="order", payload={
        "customer_id": customer["id"], "asset_id": available["id"], "amount_cents": 55_000,
    }))["orders"][-1]
    for action, payload in (
        ("payment", {"order_id": order["id"], "amount_cents": 55_000}),
        ("deposit", {"order_id": order["id"], "amount_cents": 15_000}),
        ("deliver", {"order_id": order["id"]}),
        ("return", {"order_id": order["id"]}),
        ("inspect", {"order_id": order["id"], "asset_id": available["id"], "condition_status": "cleaning_required"}),
        ("cleaning", {"order_id": order["id"], "asset_id": available["id"], "amount_cents": 5_000}),
        ("refund", {"order_id": order["id"], "amount_cents": 15_000}),
    ):
        demo = repo.demo_action(1, PulseDemoAction(action=action, payload=payload))
    detail = repo.demo_order(1, order["id"])
    assert detail["status"] == "completed"
    assert {item["payment_type"] for item in detail["payments"]} >= {"rental", "deposit", "deposit_refund"}
    assert detail["inspections"] and detail["expenses"] and detail["journals"]
    assert demo["trial_balance"]["balanced"] is True
    assert demo["statements"]["balance_sheet"]["balanced"] is True
    assert repo.demo_asset(1, available["id"])["lifetime_revenue"] >= 55_000


def test_real_order_status_drives_assigned_asset(product_store):
    repo = PulseRepository(product_store)
    customer, sku, asset = _pulse_master_data(repo)
    start = datetime.now(timezone.utc) + timedelta(days=2)
    order = repo.create_order(1, OrderWrite(customer_id=customer["id"], start_at=start,
        end_at=start + timedelta(days=2), items=[OrderLineWrite(sku_id=sku["id"], asset_id=asset["id"])]))
    repo.transition_order(1, order["id"], OrderStatusWrite(status="rented"))
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "rented"
    repo.transition_order(1, order["id"], OrderStatusWrite(status="returned"))
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "inspection"
    repo.create_inspection(1, InspectionWrite(order_id=order["id"], asset_id=asset["id"],
        condition_status="cleaning_required", resolution_status="confirmed"))
    repo.record_expense(1, ExpenseWrite(category="cleaning", amount_cents=500, order_id=order["id"],
        asset_id=asset["id"], description="Post-rental cleaning"))
    repo.transition_asset(1, asset["id"], AssetStatusWrite(status="available", order_id=order["id"]))
    repo.record_payment(1, PaymentWrite(order_id=order["id"], payment_type="deposit_refund", amount_cents=3_000))
    completed = repo.transition_order(1, order["id"], OrderStatusWrite(status="completed"))
    assert completed["status"] == "completed"
    assert completed["inspections"] and completed["expenses"] and len(completed["journals"]) == 2
    assert repo.list_entity(1, "pulse_assets")[0]["status"] == "available"


def test_product_domain_routes_work_with_postgres(persistent_app):
    """The two products must use the same production PostgreSQL adapter."""
    from fastapi.testclient import TestClient

    with TestClient(persistent_app.main.app) as client:
        headers, _ = register(client, "product-domain-postgres-user")
        material = client.post("/api/leap/materials", headers=headers, json={
            "title": "跨域阅读样本", "author": "测试", "text": "第一段。\n\n第二段。",
        })
        assert material.status_code == 201, material.text
        assert client.get("/api/leap/materials", headers=headers).json()["items"][0]["title"] == "跨域阅读样本"

        customer = client.post("/api/pulse/customers", headers=headers, json={"name": "Oia 测试客户"})
        sku = client.post("/api/pulse/skus", headers=headers, json={
            "name": "测试礼服", "current_price_cents": 12_000,
        })
        assert customer.status_code == 201, customer.text
        assert sku.status_code == 201, sku.text
        asset = client.post("/api/pulse/assets", headers=headers, json={
            "sku_id": sku.json()["id"], "asset_code": "PG-OIA-001",
            "accounting_class": "rental_asset", "purchase_cost_cents": 40_000,
        })
        assert asset.status_code == 201, asset.text
        assert client.get("/api/pulse/assets", headers=headers).json()[0]["asset_code"] == "PG-OIA-001"

        starts_at = datetime.now(timezone.utc) + timedelta(days=2)
        order = client.post("/api/pulse/orders", headers=headers, json={
            "customer_id": customer.json()["id"],
            "start_at": starts_at.isoformat(),
            "end_at": (starts_at + timedelta(days=2)).isoformat(),
            "items": [{"sku_id": sku.json()["id"], "asset_id": asset.json()["id"]}],
        })
        assert order.status_code == 201, order.text
        deposit = client.post("/api/pulse/payments", headers=headers, json={
            "order_id": order.json()["id"], "payment_type": "deposit", "amount_cents": 3_000,
        })
        assert deposit.status_code == 201, deposit.text
        statements = client.get(
            "/api/pulse/statements?date_from=2020-01-01&date_to=2035-12-31", headers=headers,
        )
        assert statements.status_code == 200, statements.text
        assert statements.json()["income_statement"]["revenue_total"] == 0
        assert statements.json()["balance_sheet"]["liabilities"]["Customer Deposits"] == 3_000
