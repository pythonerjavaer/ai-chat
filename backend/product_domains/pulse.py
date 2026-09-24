"""脉冲域: small-business rental ERP, accounting and deterministic analytics."""

import csv
import io
import json
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(value: Any) -> dict[str, Any]:
    return dict(value)


DEFAULT_ACCOUNTS = (
    ("1000", "Cash", "ASSET", "cash"),
    ("1010", "Bank", "ASSET", "cash"),
    ("1100", "Accounts Receivable", "ASSET", "receivable"),
    ("1200", "Merchandise Inventory", "ASSET", "inventory"),
    ("1300", "Rental Assets", "ASSET", "rental_asset"),
    ("1310", "Accumulated Depreciation", "ASSET", "contra_asset"),
    ("1400", "Prepaid Expenses", "ASSET", "other_asset"),
    ("2000", "Customer Deposits", "LIABILITY", "customer_deposit"),
    ("2100", "Accounts Payable", "LIABILITY", "payable"),
    ("2200", "Accrued Expenses", "LIABILITY", "other_liability"),
    ("3000", "Owner's Equity", "EQUITY", "owner_equity"),
    ("3100", "Retained Earnings", "EQUITY", "retained_earnings"),
    ("4000", "Rental Revenue", "REVENUE", "rental_revenue"),
    ("4100", "Product Sales Revenue", "REVENUE", "product_revenue"),
    ("4200", "Service Revenue", "REVENUE", "service_revenue"),
    ("4900", "Other Operating Revenue", "REVENUE", "other_revenue"),
    ("5000", "Cleaning Expense", "EXPENSE", "cleaning_expense"),
    ("5100", "Repair Expense", "EXPENSE", "repair_expense"),
    ("5200", "Delivery Expense", "EXPENSE", "delivery_expense"),
    ("5300", "Payment Processing Expense", "EXPENSE", "payment_fee"),
    ("5400", "Marketing Expense", "EXPENSE", "marketing_expense"),
    ("5500", "Rent Expense", "EXPENSE", "rent_expense"),
    ("5600", "Utilities", "EXPENSE", "utilities_expense"),
    ("5700", "Depreciation Expense", "EXPENSE", "depreciation_expense"),
    ("5900", "Other Operating Expenses", "EXPENSE", "other_expense"),
)

METRICS = (
    ("revenue", "Revenue", "已过账收入科目贷方减借方", "journal_lines + accounts", "day/month", "不含押金；仅含已过账凭证"),
    ("aov", "Average Order Value", "有效订单成交金额 / 有效订单数", "orders", "day/month", "取消订单不计入"),
    ("asset_utilization", "Asset Utilization", "出租占用天数 / 可用天数", "asset_occupancies", "day", "缺少完整可用期时为Partial"),
    ("repeat_rate", "Repeat Customer Rate", "有两笔及以上有效订单客户 / 有订单客户", "orders", "period", "只描述系统内记录"),
    ("contribution", "Contribution", "已确认收入 - 已记录直接成本", "journal_lines + asset_events", "period", "成本缺失时为Partial"),
    ("asset_roi", "Asset ROI", "资产贡献 / 采购成本", "assets + accounting", "lifetime", "采购成本或直接成本不全时不显示"),
)


def init_pulse_schema(connect: Callable[[], Any]) -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS pulse_settings (
                user_id INTEGER PRIMARY KEY,
                currency TEXT NOT NULL DEFAULT 'AUD',
                cleaning_buffer_hours INTEGER NOT NULL DEFAULT 24,
                depreciation_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_customers (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,name TEXT NOT NULL,
                phone TEXT NOT NULL DEFAULT '',email TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_skus (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,name TEXT NOT NULL,category TEXT NOT NULL DEFAULT '',
                size TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL DEFAULT 'rental',current_price_cents INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_assets (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,sku_id TEXT NOT NULL,asset_code TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'available',accounting_class TEXT NOT NULL DEFAULT 'unclassified',
                purchase_cost_cents INTEGER,purchase_cost_complete INTEGER NOT NULL DEFAULT 0,
                acquisition_date TEXT,useful_life_months INTEGER,residual_value_cents INTEGER NOT NULL DEFAULT 0,
                depreciation_method TEXT NOT NULL DEFAULT 'none',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(sku_id) REFERENCES pulse_skus(id),UNIQUE(user_id,asset_code)
            );
            CREATE TABLE IF NOT EXISTS pulse_orders (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,customer_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'reserved',
                channel TEXT NOT NULL DEFAULT '',currency TEXT NOT NULL,total_cents INTEGER NOT NULL DEFAULT 0,
                discount_cents INTEGER NOT NULL DEFAULT 0,start_at TEXT NOT NULL,end_at TEXT NOT NULL,
                delivery_method TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(customer_id) REFERENCES pulse_customers(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_order_items (
                id TEXT PRIMARY KEY,order_id TEXT NOT NULL,user_id INTEGER NOT NULL,sku_id TEXT NOT NULL,
                asset_id TEXT,quantity INTEGER NOT NULL DEFAULT 1,unit_price_cents INTEGER NOT NULL,
                line_total_cents INTEGER NOT NULL,returned_quantity INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY(order_id) REFERENCES pulse_orders(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(sku_id) REFERENCES pulse_skus(id),FOREIGN KEY(asset_id) REFERENCES pulse_assets(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_asset_occupancies (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,asset_id TEXT NOT NULL,order_id TEXT NOT NULL,
                starts_at TEXT NOT NULL,ends_at TEXT NOT NULL,buffer_ends_at TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'reserved',
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(asset_id) REFERENCES pulse_assets(id),FOREIGN KEY(order_id) REFERENCES pulse_orders(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_payments (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,order_id TEXT,payment_type TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,method TEXT NOT NULL DEFAULT '',reference TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'recorded',occurred_at TEXT NOT NULL,created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(order_id) REFERENCES pulse_orders(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_inspections (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,order_id TEXT NOT NULL,asset_id TEXT NOT NULL,
                condition_status TEXT NOT NULL,missing_items TEXT NOT NULL DEFAULT '',damage_notes TEXT NOT NULL DEFAULT '',
                resolution_status TEXT NOT NULL DEFAULT 'pending',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(order_id) REFERENCES pulse_orders(id),FOREIGN KEY(asset_id) REFERENCES pulse_assets(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_asset_events (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,asset_id TEXT NOT NULL,event_type TEXT NOT NULL,
                order_id TEXT,amount_cents INTEGER,notes TEXT NOT NULL DEFAULT '',occurred_at TEXT NOT NULL,created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(asset_id) REFERENCES pulse_assets(id),FOREIGN KEY(order_id) REFERENCES pulse_orders(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_accounts (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,code TEXT NOT NULL,name TEXT NOT NULL,
                account_type TEXT NOT NULL,role TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(user_id,code),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_accounting_events (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,event_type TEXT NOT NULL,
                source_document_type TEXT NOT NULL,source_document_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,cash_flow_category TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
                UNIQUE(user_id,event_type,source_document_type,source_document_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_journals (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,accounting_event_id TEXT,
                status TEXT NOT NULL DEFAULT 'posted',transaction_date TEXT NOT NULL,posting_date TEXT NOT NULL,
                description TEXT NOT NULL,source_document_type TEXT NOT NULL,source_document_id TEXT NOT NULL,
                reversal_of TEXT,reversed_by TEXT,created_by INTEGER NOT NULL,created_at TEXT NOT NULL,posted_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(accounting_event_id) REFERENCES pulse_accounting_events(id),
                FOREIGN KEY(reversal_of) REFERENCES pulse_journals(id),FOREIGN KEY(reversed_by) REFERENCES pulse_journals(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_journal_lines (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,journal_id TEXT NOT NULL,account_id TEXT NOT NULL,
                debit_cents INTEGER NOT NULL DEFAULT 0,credit_cents INTEGER NOT NULL DEFAULT 0,currency TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',customer_id TEXT,order_id TEXT,asset_id TEXT,vendor_id TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(journal_id) REFERENCES pulse_journals(id) ON DELETE CASCADE,
                FOREIGN KEY(account_id) REFERENCES pulse_accounts(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_vendors (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,name TEXT NOT NULL,contact TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_expenses (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,vendor_id TEXT,order_id TEXT,asset_id TEXT,
                category TEXT NOT NULL,amount_cents INTEGER NOT NULL,currency TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'paid',
                description TEXT NOT NULL DEFAULT '',occurred_at TEXT NOT NULL,created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_accounting_periods (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,period_key TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'OPEN',
                closed_at TEXT,created_at TEXT NOT NULL,UNIQUE(user_id,period_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_imports (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,import_key TEXT NOT NULL,entity_type TEXT NOT NULL,
                row_count INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(user_id,import_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_metric_dictionary (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,metric_key TEXT NOT NULL,name TEXT NOT NULL,
                definition TEXT NOT NULL,formula TEXT NOT NULL,data_source TEXT NOT NULL,time_grain TEXT NOT NULL,
                limitations TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,UNIQUE(user_id,metric_key),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_pulse_orders_user_date ON pulse_orders(user_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pulse_occupancy_asset_time ON pulse_asset_occupancies(user_id,asset_id,starts_at,buffer_ends_at,status);
            CREATE INDEX IF NOT EXISTS idx_pulse_payments_user_date ON pulse_payments(user_id,occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_pulse_journal_user_date ON pulse_journals(user_id,posting_date,status);
            CREATE INDEX IF NOT EXISTS idx_pulse_lines_account ON pulse_journal_lines(user_id,account_id,journal_id);
            CREATE INDEX IF NOT EXISTS idx_pulse_asset_events ON pulse_asset_events(user_id,asset_id,occurred_at DESC);
            """
        )


class CustomerWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=180)
    phone: str = Field(default="", max_length=60)
    email: str = Field(default="", max_length=254)
    source: str = Field(default="", max_length=120)
    notes: str = Field(default="", max_length=4_000)


class SKUWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=180)
    category: str = Field(default="", max_length=100)
    size: str = Field(default="", max_length=60)
    kind: Literal["rental", "sale", "service", "package"] = "rental"
    current_price_cents: int = Field(default=0, ge=0, le=100_000_000)


class AssetWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_id: str
    asset_code: str = Field(min_length=1, max_length=100)
    accounting_class: Literal["unclassified", "rental_asset", "inventory"] = "unclassified"
    purchase_cost_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    acquisition_date: date | None = None
    useful_life_months: int | None = Field(default=None, ge=1, le=600)
    residual_value_cents: int = Field(default=0, ge=0)
    depreciation_method: Literal["none", "straight_line"] = "none"


class OrderLineWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku_id: str
    asset_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=100)
    unit_price_cents: int | None = Field(default=None, ge=0, le=100_000_000)


class OrderWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str
    start_at: datetime
    end_at: datetime
    channel: str = Field(default="", max_length=100)
    delivery_method: str = Field(default="", max_length=100)
    discount_cents: int = Field(default=0, ge=0)
    notes: str = Field(default="", max_length=4_000)
    items: list[OrderLineWrite] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def valid_window(self):
        if self.end_at <= self.start_at:
            raise ValueError("归还时间必须晚于交付时间。")
        return self


class PaymentWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str | None = None
    payment_type: Literal["rental", "deposit", "deposit_refund", "damage_deduction", "product_sale", "service"]
    amount_cents: int = Field(gt=0, le=100_000_000)
    method: str = Field(default="", max_length=80)
    reference: str = Field(default="", max_length=180)
    occurred_at: datetime | None = None


class AssetStatusWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["available", "reserved", "rented", "returned", "inspection", "cleaning", "maintenance", "unavailable"]
    order_id: str | None = None
    notes: str = Field(default="", max_length=2_000)


class InspectionWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: str
    asset_id: str
    condition_status: Literal["good", "cleaning_required", "repair_required", "damaged", "missing"]
    missing_items: str = Field(default="", max_length=2_000)
    damage_notes: str = Field(default="", max_length=4_000)
    resolution_status: Literal["pending", "confirmed", "resolved"] = "pending"


class AccountWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1, max_length=30)
    name: str = Field(min_length=1, max_length=180)
    account_type: Literal["ASSET", "LIABILITY", "EQUITY", "REVENUE", "EXPENSE"]
    role: str = Field(default="", max_length=80)


class JournalLineWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_code: str
    debit_cents: int = Field(default=0, ge=0)
    credit_cents: int = Field(default=0, ge=0)
    description: str = Field(default="", max_length=500)
    customer_id: str | None = None
    order_id: str | None = None
    asset_id: str | None = None
    vendor_id: str | None = None

    @model_validator(mode="after")
    def one_side(self):
        if (self.debit_cents > 0) == (self.credit_cents > 0):
            raise ValueError("每条分录必须且只能有借方或贷方金额。")
        return self


class JournalWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transaction_date: date
    posting_date: date
    description: str = Field(min_length=1, max_length=500)
    source_document_type: str = Field(default="manual", max_length=80)
    source_document_id: str = Field(default="manual", max_length=120)
    cash_flow_category: Literal["", "operating", "investing", "financing"] = ""
    lines: list[JournalLineWrite] = Field(min_length=2, max_length=100)


class ExpenseWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: Literal["cleaning", "repair", "delivery", "payment_fee", "marketing", "rent", "utilities", "other"]
    amount_cents: int = Field(gt=0, le=100_000_000)
    status: Literal["unpaid", "partially_paid", "paid"] = "paid"
    vendor_id: str | None = None
    order_id: str | None = None
    asset_id: str | None = None
    description: str = Field(default="", max_length=1_000)
    occurred_at: datetime | None = None


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(default="AUD", min_length=3, max_length=3)
    cleaning_buffer_hours: int = Field(default=24, ge=0, le=336)
    depreciation_enabled: bool = False

    @field_validator("currency")
    @classmethod
    def currency_upper(cls, value: str) -> str:
        return value.upper()


_asset_write_lock = threading.Lock()


def _asset_lock(asset_ids: list[str]) -> threading.Lock:
    # The production service uses one worker. A single bounded process lock,
    # combined with the database transaction lock, prevents double-booking
    # without retaining an unbounded lock object for every asset combination.
    del asset_ids
    return _asset_write_lock


class PulseRepository:
    def __init__(self, connect: Callable[[], Any]):
        self.connect = connect

    def ensure_user_setup(self, user_id: int) -> dict:
        now = _now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO pulse_settings(user_id,updated_at) VALUES(?,?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, now),
            )
            for code, name, account_type, role in DEFAULT_ACCOUNTS:
                connection.execute(
                    """INSERT INTO pulse_accounts(id,user_id,code,name,account_type,role,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,code) DO NOTHING""",
                    (str(uuid.uuid4()), user_id, code, name, account_type, role, now, now),
                )
            for key, name, formula, source, grain, limitations in METRICS:
                connection.execute(
                    """INSERT INTO pulse_metric_dictionary
                       (id,user_id,metric_key,name,definition,formula,data_source,time_grain,limitations)
                       VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id,metric_key) DO NOTHING""",
                    (str(uuid.uuid4()), user_id, key, name, name, formula, source, grain, limitations),
                )
            row = connection.execute("SELECT * FROM pulse_settings WHERE user_id=?", (user_id,)).fetchone()
        return _row(row)

    def settings(self, user_id: int) -> dict:
        return self.ensure_user_setup(user_id)

    def update_settings(self, user_id: int, payload: SettingsWrite) -> dict:
        self.ensure_user_setup(user_id)
        with self.connect() as connection:
            connection.execute(
                """UPDATE pulse_settings SET currency=?,cleaning_buffer_hours=?,depreciation_enabled=?,updated_at=?
                   WHERE user_id=?""",
                (payload.currency, payload.cleaning_buffer_hours, int(payload.depreciation_enabled), _now(), user_id),
            )
        return self.settings(user_id)

    def _owned(self, connection: Any, table: str, item_id: str, user_id: int) -> dict:
        row = connection.execute(f"SELECT * FROM {table} WHERE id=? AND user_id=?", (item_id, user_id)).fetchone()
        if not row:
            raise KeyError("记录不存在或不属于当前账号。")
        return _row(row)

    def create_customer(self, user_id: int, payload: CustomerWrite) -> dict:
        item_id, now = str(uuid.uuid4()), _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO pulse_customers(id,user_id,name,phone,email,source,notes,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, user_id, payload.name.strip(), payload.phone.strip(), payload.email.strip(),
                 payload.source.strip(), payload.notes.strip(), now, now),
            )
            return self._owned(connection, "pulse_customers", item_id, user_id)

    def create_sku(self, user_id: int, payload: SKUWrite) -> dict:
        item_id, now = str(uuid.uuid4()), _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO pulse_skus(id,user_id,name,category,size,kind,current_price_cents,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, user_id, payload.name.strip(), payload.category.strip(), payload.size.strip(),
                 payload.kind, payload.current_price_cents, now, now),
            )
            return self._owned(connection, "pulse_skus", item_id, user_id)

    def create_asset(self, user_id: int, payload: AssetWrite) -> dict:
        item_id, now = str(uuid.uuid4()), _now()
        with self.connect() as connection:
            self._owned(connection, "pulse_skus", payload.sku_id, user_id)
            connection.execute(
                """INSERT INTO pulse_assets
                   (id,user_id,sku_id,asset_code,accounting_class,purchase_cost_cents,purchase_cost_complete,
                    acquisition_date,useful_life_months,residual_value_cents,depreciation_method,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, user_id, payload.sku_id, payload.asset_code.strip(), payload.accounting_class,
                 payload.purchase_cost_cents, int(payload.purchase_cost_cents is not None),
                 payload.acquisition_date.isoformat() if payload.acquisition_date else None,
                 payload.useful_life_months, payload.residual_value_cents, payload.depreciation_method, now, now),
            )
            connection.execute(
                """INSERT INTO pulse_asset_events(id,user_id,asset_id,event_type,amount_cents,notes,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), user_id, item_id, "acquired", payload.purchase_cost_cents,
                 "资产登记；是否生成采购会计分录取决于实际付款/应付记录。", now, now),
            )
            return self._owned(connection, "pulse_assets", item_id, user_id)

    def list_entity(self, user_id: int, table: str, order_by: str = "updated_at DESC", limit: int = 100) -> list[dict]:
        allowed = {"pulse_customers", "pulse_skus", "pulse_assets", "pulse_orders", "pulse_payments", "pulse_inspections", "pulse_expenses"}
        if table not in allowed:
            raise ValueError("不支持的列表。")
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE user_id=? ORDER BY {order_by} LIMIT ?", (user_id, limit),
            ).fetchall()
        return [_row(item) for item in rows]

    def create_order(self, user_id: int, payload: OrderWrite) -> dict:
        self.ensure_user_setup(user_id)
        asset_ids = [item.asset_id for item in payload.items if item.asset_id]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("同一订单不能重复占用同一件实物资产。")
        with _asset_lock(asset_ids):
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._owned(connection, "pulse_customers", payload.customer_id, user_id)
                settings = connection.execute("SELECT * FROM pulse_settings WHERE user_id=?", (user_id,)).fetchone()
                order_id, now, total = str(uuid.uuid4()), _now(), 0
                lines: list[tuple] = []
                buffer_seconds = int(settings["cleaning_buffer_hours"]) * 3600
                buffer_end = datetime.fromtimestamp(payload.end_at.timestamp() + buffer_seconds, timezone.utc).isoformat()
                start, end = payload.start_at.astimezone(timezone.utc).isoformat(), payload.end_at.astimezone(timezone.utc).isoformat()
                for item in payload.items:
                    sku = self._owned(connection, "pulse_skus", item.sku_id, user_id)
                    unit = sku["current_price_cents"] if item.unit_price_cents is None else item.unit_price_cents
                    line_total, line_id = unit * item.quantity, str(uuid.uuid4())
                    if item.asset_id:
                        asset = self._owned(connection, "pulse_assets", item.asset_id, user_id)
                        if asset["sku_id"] != item.sku_id or asset["status"] in {"maintenance", "unavailable"}:
                            raise ValueError("资产与SKU不一致或当前不可租。")
                        clash = connection.execute(
                            """SELECT 1 FROM pulse_asset_occupancies WHERE user_id=? AND asset_id=?
                               AND status IN ('reserved','rented') AND starts_at < ? AND buffer_ends_at > ? LIMIT 1""",
                            (user_id, item.asset_id, buffer_end, start),
                        ).fetchone()
                        if clash:
                            raise ValueError(f"资产 {asset['asset_code']} 在所选时间段已被占用。")
                    lines.append((line_id, order_id, user_id, item.sku_id, item.asset_id,
                                  item.quantity, unit, line_total))
                    total += line_total
                total = max(0, total - payload.discount_cents)
                connection.execute(
                    """INSERT INTO pulse_orders
                       (id,user_id,customer_id,status,channel,currency,total_cents,discount_cents,start_at,end_at,
                        delivery_method,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (order_id, user_id, payload.customer_id, "reserved", payload.channel.strip(), settings["currency"],
                     total, payload.discount_cents, start, end, payload.delivery_method.strip(), payload.notes.strip(), now, now),
                )
                connection.executemany(
                    """INSERT INTO pulse_order_items
                       (id,order_id,user_id,sku_id,asset_id,quantity,unit_price_cents,line_total_cents)
                       VALUES(?,?,?,?,?,?,?,?)""", lines,
                )
                for line in lines:
                    asset_id = line[4]
                    if not asset_id:
                        continue
                    connection.execute(
                        """INSERT INTO pulse_asset_occupancies
                           (id,user_id,asset_id,order_id,starts_at,ends_at,buffer_ends_at,status,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), user_id, asset_id, order_id, start, end, buffer_end, "reserved", now, now),
                    )
                    connection.execute("UPDATE pulse_assets SET status='reserved',updated_at=? WHERE id=? AND user_id=?",
                                       (now, asset_id, user_id))
                    connection.execute(
                        """INSERT INTO pulse_asset_events(id,user_id,asset_id,event_type,order_id,notes,occurred_at,created_at)
                           VALUES(?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), user_id, asset_id, "reserved", order_id, "订单占用", now, now),
                    )
        return self.order_detail(user_id, order_id)

    def order_detail(self, user_id: int, order_id: str) -> dict:
        with self.connect() as connection:
            order = self._owned(connection, "pulse_orders", order_id, user_id)
            customer = self._owned(connection, "pulse_customers", order["customer_id"], user_id)
            items = connection.execute(
                """SELECT i.*,s.name AS sku_name,a.asset_code FROM pulse_order_items i
                   JOIN pulse_skus s ON s.id=i.sku_id LEFT JOIN pulse_assets a ON a.id=i.asset_id
                   WHERE i.order_id=? AND i.user_id=? ORDER BY i.id""", (order_id, user_id),
            ).fetchall()
            payments = connection.execute(
                "SELECT * FROM pulse_payments WHERE order_id=? AND user_id=? ORDER BY occurred_at", (order_id, user_id),
            ).fetchall()
            journals = connection.execute(
                """SELECT j.id,j.description,j.posting_date,j.status FROM pulse_journals j
                   WHERE j.user_id=? AND j.source_document_id IN
                   (SELECT id FROM pulse_payments WHERE order_id=? AND user_id=?) ORDER BY j.posting_date""",
                (user_id, order_id, user_id),
            ).fetchall()
        order.update(customer={k: customer[k] for k in ("id", "name", "phone", "email")},
                     items=[_row(x) for x in items], payments=[_row(x) for x in payments],
                     journals=[_row(x) for x in journals])
        return order

    def transition_asset(self, user_id: int, asset_id: str, payload: AssetStatusWrite) -> dict:
        now = _now()
        with self.connect() as connection:
            asset = self._owned(connection, "pulse_assets", asset_id, user_id)
            if payload.order_id:
                self._owned(connection, "pulse_orders", payload.order_id, user_id)
            allowed = {
                "reserved": {"rented", "available"}, "rented": {"returned", "inspection"},
                "returned": {"inspection"}, "inspection": {"cleaning", "maintenance", "available"},
                "cleaning": {"available", "maintenance"}, "maintenance": {"available", "unavailable"},
                "available": {"reserved", "maintenance", "unavailable"}, "unavailable": {"available", "maintenance"},
            }
            if payload.status != asset["status"] and payload.status not in allowed.get(asset["status"], set()):
                raise ValueError(f"资产不能从 {asset['status']} 直接变为 {payload.status}。")
            connection.execute("UPDATE pulse_assets SET status=?,updated_at=? WHERE id=? AND user_id=?",
                               (payload.status, now, asset_id, user_id))
            if payload.order_id and payload.status in {"rented", "returned", "inspection", "available"}:
                occupancy_status = "rented" if payload.status == "rented" else "completed" if payload.status in {"returned", "inspection", "available"} else payload.status
                connection.execute(
                    "UPDATE pulse_asset_occupancies SET status=?,updated_at=? WHERE order_id=? AND asset_id=? AND user_id=?",
                    (occupancy_status, now, payload.order_id, asset_id, user_id),
                )
            connection.execute(
                """INSERT INTO pulse_asset_events(id,user_id,asset_id,event_type,order_id,notes,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (str(uuid.uuid4()), user_id, asset_id, payload.status, payload.order_id, payload.notes.strip(), now, now),
            )
            return self._owned(connection, "pulse_assets", asset_id, user_id)

    def create_inspection(self, user_id: int, payload: InspectionWrite) -> dict:
        now, item_id = _now(), str(uuid.uuid4())
        target = {"good": "available", "cleaning_required": "cleaning", "repair_required": "maintenance",
                  "damaged": "maintenance", "missing": "unavailable"}[payload.condition_status]
        with self.connect() as connection:
            self._owned(connection, "pulse_orders", payload.order_id, user_id)
            self._owned(connection, "pulse_assets", payload.asset_id, user_id)
            connection.execute(
                """INSERT INTO pulse_inspections
                   (id,user_id,order_id,asset_id,condition_status,missing_items,damage_notes,resolution_status,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item_id, user_id, payload.order_id, payload.asset_id, payload.condition_status,
                 payload.missing_items.strip(), payload.damage_notes.strip(), payload.resolution_status, now, now),
            )
            if payload.resolution_status == "confirmed":
                connection.execute("UPDATE pulse_assets SET status=?,updated_at=? WHERE id=? AND user_id=?",
                                   (target, now, payload.asset_id, user_id))
                connection.execute(
                    """INSERT INTO pulse_asset_events(id,user_id,asset_id,event_type,order_id,notes,occurred_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), user_id, payload.asset_id, target, payload.order_id,
                     payload.damage_notes.strip() or payload.missing_items.strip(), now, now),
                )
            return self._owned(connection, "pulse_inspections", item_id, user_id)

    def _account(self, connection: Any, user_id: int, *, code: str | None = None, role: str | None = None) -> dict:
        key, value = ("code", code) if code else ("role", role)
        row = connection.execute(
            f"SELECT * FROM pulse_accounts WHERE user_id=? AND {key}=? AND active=1", (user_id, value),
        ).fetchone()
        if not row:
            raise ValueError(f"缺少会计科目：{value}")
        return _row(row)

    def _period_open(self, connection: Any, user_id: int, posting_date: str) -> None:
        period = posting_date[:7]
        row = connection.execute(
            "SELECT status FROM pulse_accounting_periods WHERE user_id=? AND period_key=?", (user_id, period),
        ).fetchone()
        if row and row["status"] == "CLOSED":
            raise ValueError(f"会计期间 {period} 已关闭，请使用冲销和更正凭证。")

    def post_journal(self, user_id: int, payload: JournalWrite, *, accounting_event_id: str | None = None) -> dict:
        self.ensure_user_setup(user_id)
        debit = sum(x.debit_cents for x in payload.lines)
        credit = sum(x.credit_cents for x in payload.lines)
        if debit != credit or debit <= 0:
            raise ValueError("凭证借贷不平衡，禁止过账。")
        journal_id, now, posting = str(uuid.uuid4()), _now(), payload.posting_date.isoformat()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._period_open(connection, user_id, posting)
            account_ids: list[str] = []
            for line in payload.lines:
                account_ids.append(self._account(connection, user_id, code=line.account_code)["id"])
            connection.execute(
                """INSERT INTO pulse_journals
                   (id,user_id,accounting_event_id,status,transaction_date,posting_date,description,
                    source_document_type,source_document_id,created_by,created_at,posted_at)
                   VALUES(?,?,?,'posted',?,?,?,?,?,?,?,?)""",
                (journal_id, user_id, accounting_event_id, payload.transaction_date.isoformat(), posting,
                 payload.description.strip(), payload.source_document_type, payload.source_document_id,
                 user_id, now, now),
            )
            settings = connection.execute("SELECT currency FROM pulse_settings WHERE user_id=?", (user_id,)).fetchone()
            connection.executemany(
                """INSERT INTO pulse_journal_lines
                   (id,user_id,journal_id,account_id,debit_cents,credit_cents,currency,description,
                    customer_id,order_id,asset_id,vendor_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(str(uuid.uuid4()), user_id, journal_id, account_ids[index], line.debit_cents,
                  line.credit_cents, settings["currency"], line.description.strip(), line.customer_id,
                  line.order_id, line.asset_id, line.vendor_id) for index, line in enumerate(payload.lines)],
            )
        return self.journal_detail(user_id, journal_id)

    def _business_journal(self, user_id: int, event_type: str, source_type: str, source_id: str,
                          occurred_at: datetime, description: str, debit_role: str, credit_role: str,
                          amount: int, cash_flow_category: str, refs: dict[str, str | None]) -> dict:
        self.ensure_user_setup(user_id)
        with self.connect() as connection:
            existing = connection.execute(
                """SELECT j.id FROM pulse_accounting_events e JOIN pulse_journals j ON j.accounting_event_id=e.id
                   WHERE e.user_id=? AND e.event_type=? AND e.source_document_type=? AND e.source_document_id=?""",
                (user_id, event_type, source_type, source_id),
            ).fetchone()
            if existing:
                return self.journal_detail(user_id, existing["id"])
            debit_code = self._account(connection, user_id, role=debit_role)["code"]
            credit_code = self._account(connection, user_id, role=credit_role)["code"]
            event_id, now = str(uuid.uuid4()), _now()
            connection.execute(
                """INSERT INTO pulse_accounting_events
                   (id,user_id,event_type,source_document_type,source_document_id,occurred_at,cash_flow_category,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (event_id, user_id, event_type, source_type, source_id, occurred_at.isoformat(), cash_flow_category, now),
            )
        payload = JournalWrite(
            transaction_date=occurred_at.date(), posting_date=occurred_at.date(), description=description,
            source_document_type=source_type, source_document_id=source_id, cash_flow_category=cash_flow_category,
            lines=[JournalLineWrite(account_code=debit_code, debit_cents=amount, **refs),
                   JournalLineWrite(account_code=credit_code, credit_cents=amount, **refs)],
        )
        return self.post_journal(user_id, payload, accounting_event_id=event_id)

    def record_payment(self, user_id: int, payload: PaymentWrite) -> dict:
        settings = self.ensure_user_setup(user_id)
        occurred = payload.occurred_at or datetime.now(timezone.utc)
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        payment_id, now = str(uuid.uuid4()), _now()
        with self.connect() as connection:
            if payload.order_id:
                self._owned(connection, "pulse_orders", payload.order_id, user_id)
            connection.execute(
                """INSERT INTO pulse_payments
                   (id,user_id,order_id,payment_type,amount_cents,currency,method,reference,status,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (payment_id, user_id, payload.order_id, payload.payment_type, payload.amount_cents,
                 settings["currency"], payload.method.strip(), payload.reference.strip(), "recorded", occurred.isoformat(), now),
            )
        mapping = {
            "rental": ("cash", "rental_revenue", "Rental payment", "operating"),
            "deposit": ("cash", "customer_deposit", "Customer deposit received", "operating"),
            "deposit_refund": ("customer_deposit", "cash", "Customer deposit refunded", "operating"),
            "damage_deduction": ("customer_deposit", "other_revenue", "Confirmed damage deduction", "operating"),
            "product_sale": ("cash", "product_revenue", "Product sale receipt", "operating"),
            "service": ("cash", "service_revenue", "Service receipt", "operating"),
        }
        debit, credit, description, cashflow = mapping[payload.payment_type]
        journal = self._business_journal(
            user_id, payload.payment_type, "payment", payment_id, occurred, description,
            debit, credit, payload.amount_cents, cashflow,
            {"order_id": payload.order_id},
        )
        return {"payment": self.get_payment(user_id, payment_id), "journal": journal}

    def get_payment(self, user_id: int, payment_id: str) -> dict:
        with self.connect() as connection:
            return self._owned(connection, "pulse_payments", payment_id, user_id)

    def record_expense(self, user_id: int, payload: ExpenseWrite) -> dict:
        settings, item_id, now = self.ensure_user_setup(user_id), str(uuid.uuid4()), _now()
        occurred = payload.occurred_at or datetime.now(timezone.utc)
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        with self.connect() as connection:
            if payload.asset_id:
                self._owned(connection, "pulse_assets", payload.asset_id, user_id)
            connection.execute(
                """INSERT INTO pulse_expenses
                   (id,user_id,vendor_id,order_id,asset_id,category,amount_cents,currency,status,description,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, user_id, payload.vendor_id, payload.order_id, payload.asset_id, payload.category,
                 payload.amount_cents, settings["currency"], payload.status, payload.description.strip(), occurred.isoformat(), now),
            )
            if payload.asset_id:
                connection.execute(
                    """INSERT INTO pulse_asset_events(id,user_id,asset_id,event_type,order_id,amount_cents,notes,occurred_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), user_id, payload.asset_id, payload.category, payload.order_id,
                     payload.amount_cents, payload.description.strip(), occurred.isoformat(), now),
                )
        roles = {"cleaning": "cleaning_expense", "repair": "repair_expense", "delivery": "delivery_expense",
                 "payment_fee": "payment_fee", "marketing": "marketing_expense", "rent": "rent_expense",
                 "utilities": "utilities_expense", "other": "other_expense"}
        credit = "cash" if payload.status == "paid" else "payable"
        journal = self._business_journal(user_id, "expense", "expense", item_id, occurred,
                                         f"{payload.category} expense", roles[payload.category], credit,
                                         payload.amount_cents, "operating" if payload.status == "paid" else "",
                                         {"order_id": payload.order_id, "asset_id": payload.asset_id,
                                          "vendor_id": payload.vendor_id})
        with self.connect() as connection:
            saved = self._owned(connection, "pulse_expenses", item_id, user_id)
        return {"expense": saved, "journal": journal}

    def journal_detail(self, user_id: int, journal_id: str) -> dict:
        with self.connect() as connection:
            journal = self._owned(connection, "pulse_journals", journal_id, user_id)
            lines = connection.execute(
                """SELECT l.*,a.code AS account_code,a.name AS account_name,a.account_type
                   FROM pulse_journal_lines l JOIN pulse_accounts a ON a.id=l.account_id
                   WHERE l.journal_id=? AND l.user_id=? ORDER BY a.code""", (journal_id, user_id),
            ).fetchall()
        journal["lines"] = [_row(item) for item in lines]
        journal["balanced"] = sum(x["debit_cents"] for x in journal["lines"]) == sum(x["credit_cents"] for x in journal["lines"])
        return journal

    def accounts(self, user_id: int) -> list[dict]:
        self.ensure_user_setup(user_id)
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM pulse_accounts WHERE user_id=? ORDER BY code", (user_id,)).fetchall()
        return [_row(item) for item in rows]

    def create_account(self, user_id: int, payload: AccountWrite) -> dict:
        self.ensure_user_setup(user_id)
        item_id, now = str(uuid.uuid4()), _now()
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO pulse_accounts
                   (id,user_id,code,name,account_type,role,active,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,1,?,?)""",
                (item_id, user_id, payload.code.strip(), payload.name.strip(),
                 payload.account_type, payload.role.strip(), now, now),
            )
            return self._owned(connection, "pulse_accounts", item_id, user_id)

    def general_ledger(self, user_id: int, date_from: str, date_to: str, account_code: str | None = None) -> dict:
        self.ensure_user_setup(user_id)
        params: list[Any] = [date_from, user_id, date_from, date_to]
        account_filter = ""
        if account_code:
            account_filter = " AND a.code=?"
            params.append(account_code)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT a.code,a.name,a.account_type,
                    COALESCE(SUM(CASE WHEN j.posting_date < ? THEN l.debit_cents-l.credit_cents ELSE 0 END),0) AS opening_balance,
                    COALESCE(SUM(CASE WHEN j.posting_date >= ? AND j.posting_date <= ? THEN l.debit_cents ELSE 0 END),0) AS debit,
                    COALESCE(SUM(CASE WHEN j.posting_date >= ? AND j.posting_date <= ? THEN l.credit_cents ELSE 0 END),0) AS credit,
                    COALESCE(SUM(CASE WHEN j.posting_date <= ? THEN l.debit_cents-l.credit_cents ELSE 0 END),0) AS closing_balance
                    FROM pulse_accounts a LEFT JOIN pulse_journal_lines l ON l.account_id=a.id AND l.user_id=a.user_id
                    LEFT JOIN pulse_journals j ON j.id=l.journal_id AND j.status='posted'
                    WHERE a.user_id=? {account_filter} GROUP BY a.id,a.code,a.name,a.account_type ORDER BY a.code""",
                (date_from, date_from, date_to, date_from, date_to, date_to, user_id) + ((account_code,) if account_code else ()),
            ).fetchall()
        return {"date_from": date_from, "date_to": date_to, "accounts": [_row(x) for x in rows]}

    def trial_balance(self, user_id: int, date_from: str, date_to: str) -> dict:
        ledger = self.general_ledger(user_id, date_from, date_to)
        total_debit = sum(item["debit"] for item in ledger["accounts"])
        total_credit = sum(item["credit"] for item in ledger["accounts"])
        ledger.update(total_debit=total_debit, total_credit=total_credit,
                      balanced=total_debit == total_credit,
                      integrity_status="balanced" if total_debit == total_credit else "Accounting Integrity Error")
        return ledger

    def statements(self, user_id: int, date_from: str, date_to: str) -> dict:
        ledger = self.general_ledger(user_id, date_from, date_to)["accounts"]
        revenue = {x["name"]: x["credit"] - x["debit"] for x in ledger if x["account_type"] == "REVENUE"}
        expense = {x["name"]: x["debit"] - x["credit"] for x in ledger if x["account_type"] == "EXPENSE"}
        assets = {x["name"]: x["closing_balance"] for x in ledger if x["account_type"] == "ASSET"}
        liabilities = {x["name"]: -x["closing_balance"] for x in ledger if x["account_type"] == "LIABILITY"}
        equity = {x["name"]: -x["closing_balance"] for x in ledger if x["account_type"] == "EQUITY"}
        revenue_total, expense_total = sum(revenue.values()), sum(expense.values())
        current_earnings = revenue_total - expense_total
        cash_roles = {"cash", "bank"}
        with self.connect() as connection:
            cash = connection.execute(
                """SELECT e.cash_flow_category,
                   COALESCE(SUM(CASE WHEN a.role IN ('cash','bank') THEN l.debit_cents-l.credit_cents ELSE 0 END),0) AS amount
                   FROM pulse_accounting_events e JOIN pulse_journals j ON j.accounting_event_id=e.id AND j.status='posted'
                   JOIN pulse_journal_lines l ON l.journal_id=j.id JOIN pulse_accounts a ON a.id=l.account_id
                   WHERE e.user_id=? AND j.posting_date>=? AND j.posting_date<=? GROUP BY e.cash_flow_category""",
                (user_id, date_from, date_to),
            ).fetchall()
            incomplete_assets = connection.execute(
                "SELECT COUNT(*) AS n FROM pulse_assets WHERE user_id=? AND purchase_cost_complete=0", (user_id,),
            ).fetchone()["n"]
        cash_flow = {item["cash_flow_category"] or "unclassified": item["amount"] for item in cash}
        asset_total, liability_total, equity_total = sum(assets.values()), sum(liabilities.values()), sum(equity.values()) + current_earnings
        return {
            "date_from": date_from, "date_to": date_to,
            "income_statement": {"revenue": revenue, "expenses": expense, "revenue_total": revenue_total,
                                 "operating_profit": current_earnings,
                                 "data_quality": "complete" if not incomplete_assets else "partial",
                                 "note": "Cost data incomplete" if incomplete_assets else "Recorded operational costs only"},
            "balance_sheet": {"assets": assets, "liabilities": liabilities,
                              "equity": {**equity, "Current Earnings": current_earnings},
                              "assets_total": asset_total, "liabilities_and_equity_total": liability_total + equity_total,
                              "balanced": asset_total == liability_total + equity_total,
                              "note": "Opening balance incomplete" if asset_total != liability_total + equity_total else "Balanced"},
            "cash_flow": {"method": "direct", "operating": cash_flow.get("operating", 0),
                          "investing": cash_flow.get("investing", 0), "financing": cash_flow.get("financing", 0),
                          "unclassified": cash_flow.get("unclassified", 0)},
        }

    def dashboard(self, user_id: int, date_from: str, date_to: str) -> dict:
        statements = self.statements(user_id, date_from, date_to)
        with self.connect() as connection:
            metrics = connection.execute(
                """SELECT COUNT(*) AS orders,
                   COALESCE(SUM(CASE WHEN status!='cancelled' THEN total_cents ELSE 0 END),0) AS order_value,
                   COALESCE(SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END),0) AS cancelled
                   FROM pulse_orders WHERE user_id=? AND substr(created_at,1,10)>=? AND substr(created_at,1,10)<=?""",
                (user_id, date_from, date_to),
            ).fetchone()
            payment = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN payment_type IN ('rental','product_sale','service','deposit') THEN amount_cents ELSE 0 END),0) AS cash_in,
                   COALESCE(SUM(CASE WHEN payment_type='deposit_refund' THEN amount_cents ELSE 0 END),0) AS cash_out,
                   COALESCE(SUM(CASE WHEN payment_type='deposit' THEN amount_cents WHEN payment_type IN ('deposit_refund','damage_deduction') THEN -amount_cents ELSE 0 END),0) AS deposits_held
                   FROM pulse_payments WHERE user_id=? AND substr(occurred_at,1,10)>=? AND substr(occurred_at,1,10)<=?""",
                (user_id, date_from, date_to),
            ).fetchone()
            assets = connection.execute(
                """SELECT COUNT(*) AS assets,COALESCE(SUM(CASE WHEN status='available' THEN 1 ELSE 0 END),0) AS available,
                   COALESCE(SUM(CASE WHEN status IN ('reserved','rented') THEN 1 ELSE 0 END),0) AS occupied,
                   COALESCE(SUM(CASE WHEN purchase_cost_complete=1 THEN purchase_cost_cents ELSE 0 END),0) AS recorded_asset_cost,
                   COALESCE(SUM(CASE WHEN purchase_cost_complete=0 THEN 1 ELSE 0 END),0) AS incomplete_cost_count
                   FROM pulse_assets WHERE user_id=?""", (user_id,),
            ).fetchone()
            repeats = connection.execute(
                """SELECT COUNT(*) AS customers,COALESCE(SUM(CASE WHEN order_count>=2 THEN 1 ELSE 0 END),0) AS repeaters
                   FROM (SELECT customer_id,COUNT(*) AS order_count FROM pulse_orders
                         WHERE user_id=? AND status!='cancelled' GROUP BY customer_id) q""", (user_id,),
            ).fetchone()
        order_count = int(metrics["orders"])
        asset_count = int(assets["assets"])
        return {"date_from": date_from, "date_to": date_to, "currency": self.settings(user_id)["currency"],
                "metrics": {"revenue": statements["income_statement"]["revenue_total"], "orders": order_count,
                            "cash_in": payment["cash_in"], "cash_out": payment["cash_out"],
                            "operating_expenses": sum(statements["income_statement"]["expenses"].values()),
                            "operating_profit": statements["income_statement"]["operating_profit"],
                            "deposits_held": payment["deposits_held"], "rental_asset_value": assets["recorded_asset_cost"],
                            "inventory_availability": round(int(assets["available"]) * 100 / max(1, asset_count), 1),
                            "asset_utilization_proxy": round(int(assets["occupied"]) * 100 / max(1, asset_count), 1),
                            "average_order_value": round(int(metrics["order_value"]) / max(1, order_count)),
                            "repeat_customer_rate": round(int(repeats["repeaters"]) * 100 / max(1, int(repeats["customers"])), 1)},
                "data_quality": {"profitability": "partial" if assets["incomplete_cost_count"] else "complete",
                                 "asset_cost_incomplete": assets["incomplete_cost_count"],
                                 "lost_demand": "insufficient_data", "forecast": "not_enabled"}}

    def asset_economics(self, user_id: int, asset_id: str) -> dict:
        with self.connect() as connection:
            asset = self._owned(connection, "pulse_assets", asset_id, user_id)
            stats = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN p.payment_type='rental' THEN p.amount_cents ELSE 0 END),0) AS revenue,
                   COUNT(DISTINCT CASE WHEN p.payment_type='rental' THEN p.order_id END) AS rental_count
                   FROM pulse_payments p JOIN pulse_order_items i ON i.order_id=p.order_id
                   WHERE p.user_id=? AND i.asset_id=?""", (user_id, asset_id),
            ).fetchone()
            costs = connection.execute(
                """SELECT COALESCE(SUM(CASE WHEN event_type='cleaning' THEN amount_cents ELSE 0 END),0) AS cleaning_cost,
                   COALESCE(SUM(CASE WHEN event_type='repair' THEN amount_cents ELSE 0 END),0) AS repair_cost
                   FROM pulse_asset_events WHERE user_id=? AND asset_id=?""", (user_id, asset_id),
            ).fetchone()
            history = connection.execute(
                "SELECT * FROM pulse_asset_events WHERE user_id=? AND asset_id=? ORDER BY occurred_at DESC LIMIT 100",
                (user_id, asset_id),
            ).fetchall()
        revenue, direct_cost = int(stats["revenue"]), int(costs["cleaning_cost"]) + int(costs["repair_cost"])
        complete = bool(asset["purchase_cost_complete"])
        contribution = revenue - direct_cost
        return {"asset": asset, "lifetime_revenue": revenue, "rental_count": stats["rental_count"],
                "average_revenue_per_rental": round(revenue / max(1, int(stats["rental_count"]))),
                "cleaning_cost": costs["cleaning_cost"], "repair_cost": costs["repair_cost"],
                "contribution": contribution, "roi": round(contribution / asset["purchase_cost_cents"], 4)
                if complete and asset["purchase_cost_cents"] else None,
                "data_quality": "complete" if complete else "insufficient_cost_data",
                "history": [_row(item) for item in history]}

    def analytics(self, user_id: int, date_from: str, date_to: str) -> dict:
        with self.connect() as connection:
            revenue_by_sku = connection.execute(
                """SELECT s.id,s.name,COALESCE(SUM(p.amount_cents),0) AS revenue,COUNT(DISTINCT o.id) AS orders
                   FROM pulse_skus s LEFT JOIN pulse_order_items i ON i.sku_id=s.id
                   LEFT JOIN pulse_orders o ON o.id=i.order_id AND o.user_id=s.user_id
                   LEFT JOIN pulse_payments p ON p.order_id=o.id AND p.payment_type='rental'
                    AND substr(p.occurred_at,1,10)>=? AND substr(p.occurred_at,1,10)<=?
                   WHERE s.user_id=? GROUP BY s.id,s.name ORDER BY revenue DESC LIMIT 50""",
                (date_from, date_to, user_id),
            ).fetchall()
            customers = connection.execute(
                """SELECT c.id,c.name,COUNT(DISTINCT o.id) AS orders,
                   COALESCE(SUM(CASE WHEN p.payment_type IN ('rental','product_sale','service') THEN p.amount_cents ELSE 0 END),0) AS lifetime_revenue,
                   MAX(o.created_at) AS last_order_at FROM pulse_customers c
                   LEFT JOIN pulse_orders o ON o.customer_id=c.id AND o.user_id=c.user_id
                   LEFT JOIN pulse_payments p ON p.order_id=o.id
                   WHERE c.user_id=? GROUP BY c.id,c.name ORDER BY lifetime_revenue DESC LIMIT 100""", (user_id,),
            ).fetchall()
        customer_rows = []
        for item in customers:
            row = _row(item)
            row["segment"] = "High Value" if row["lifetime_revenue"] >= 100_000 else "Frequent" if row["orders"] >= 3 else "Returning" if row["orders"] >= 2 else "New"
            row["segment_rule"] = "High Value≥1000 currency units; Frequent≥3 orders; Returning=2; otherwise New"
            customer_rows.append(row)
        return {"date_from": date_from, "date_to": date_to,
                "sales": {"revenue_by_sku": [_row(x) for x in revenue_by_sku]},
                "customers": customer_rows,
                "limitations": {"cohort": "planned", "rfm": "rule_segments_only", "funnel": "insufficient_data",
                                "forecasting": "not_enabled", "lost_demand": "unavailable_without_demand_events"}}

    def metric_dictionary(self, user_id: int) -> list[dict]:
        self.ensure_user_setup(user_id)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pulse_metric_dictionary WHERE user_id=? AND active=1 ORDER BY metric_key", (user_id,),
            ).fetchall()
        return [_row(x) for x in rows]


def create_pulse_router(connect: Callable[[], Any], current_user: Callable[..., dict]) -> APIRouter:
    router, repo = APIRouter(prefix="/api/pulse", tags=["脉冲域"]), PulseRepository(connect)
    User = Annotated[dict, Depends(current_user)]

    def safe(call):
        try:
            return call()
        except KeyError as exc:
            raise HTTPException(404, detail=str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc)) from exc

    @router.get("/capabilities")
    def capabilities(_: User):
        return {"model_calls": False, "paid_apis": False, "tax_module": False,
                "implemented": ["customers", "orders", "reservations", "assets", "payments", "deposits",
                                "returns", "inspections", "double_entry", "general_ledger", "trial_balance",
                                "financial_statements", "executive_dashboard", "customer_analytics",
                                "asset_economics", "metric_dictionary"],
                "experimental": ["rule_customer_segments"],
                "planned": ["cohort", "full_rfm", "funnel", "demand_events", "scenario_analysis", "forecasting"]}

    @router.get("/settings")
    def settings(user: User): return repo.settings(user["id"])

    @router.put("/settings")
    def update_settings(payload: SettingsWrite, user: User): return repo.update_settings(user["id"], payload)

    @router.get("/customers")
    def customers(user: User): return repo.list_entity(user["id"], "pulse_customers")

    @router.post("/customers", status_code=201)
    def create_customer(payload: CustomerWrite, user: User): return safe(lambda: repo.create_customer(user["id"], payload))

    @router.get("/skus")
    def skus(user: User): return repo.list_entity(user["id"], "pulse_skus")

    @router.post("/skus", status_code=201)
    def create_sku(payload: SKUWrite, user: User): return safe(lambda: repo.create_sku(user["id"], payload))

    @router.get("/assets")
    def assets(user: User): return repo.list_entity(user["id"], "pulse_assets")

    @router.post("/assets", status_code=201)
    def create_asset(payload: AssetWrite, user: User): return safe(lambda: repo.create_asset(user["id"], payload))

    @router.get("/assets/{asset_id}/economics")
    def asset_economics(asset_id: str, user: User): return safe(lambda: repo.asset_economics(user["id"], asset_id))

    @router.post("/assets/{asset_id}/status")
    def asset_status(asset_id: str, payload: AssetStatusWrite, user: User): return safe(lambda: repo.transition_asset(user["id"], asset_id, payload))

    @router.get("/orders")
    def orders(user: User): return repo.list_entity(user["id"], "pulse_orders", "created_at DESC")

    @router.post("/orders", status_code=201)
    def create_order(payload: OrderWrite, user: User): return safe(lambda: repo.create_order(user["id"], payload))

    @router.get("/orders/{order_id}")
    def order_detail(order_id: str, user: User): return safe(lambda: repo.order_detail(user["id"], order_id))

    @router.post("/payments", status_code=201)
    def payment(payload: PaymentWrite, user: User): return safe(lambda: repo.record_payment(user["id"], payload))

    @router.get("/payments")
    def payments(user: User): return repo.list_entity(user["id"], "pulse_payments", "occurred_at DESC")

    @router.post("/inspections", status_code=201)
    def inspection(payload: InspectionWrite, user: User): return safe(lambda: repo.create_inspection(user["id"], payload))

    @router.get("/inspections")
    def inspections(user: User): return repo.list_entity(user["id"], "pulse_inspections")

    @router.post("/expenses", status_code=201)
    def expense(payload: ExpenseWrite, user: User): return safe(lambda: repo.record_expense(user["id"], payload))

    @router.get("/expenses")
    def expenses(user: User): return repo.list_entity(user["id"], "pulse_expenses", "occurred_at DESC")

    @router.get("/accounts")
    def accounts(user: User): return repo.accounts(user["id"])

    @router.post("/accounts", status_code=201)
    def create_account(payload: AccountWrite, user: User):
        return safe(lambda: repo.create_account(user["id"], payload))

    @router.post("/journals", status_code=201)
    def journal(payload: JournalWrite, user: User): return safe(lambda: repo.post_journal(user["id"], payload))

    @router.get("/journals/{journal_id}")
    def journal_detail(journal_id: str, user: User): return safe(lambda: repo.journal_detail(user["id"], journal_id))

    @router.get("/general-ledger")
    def ledger(user: User, date_from: str, date_to: str, account_code: str | None = None):
        return safe(lambda: repo.general_ledger(user["id"], date_from, date_to, account_code))

    @router.get("/trial-balance")
    def trial_balance(user: User, date_from: str, date_to: str): return repo.trial_balance(user["id"], date_from, date_to)

    @router.get("/statements")
    def statements(user: User, date_from: str, date_to: str): return repo.statements(user["id"], date_from, date_to)

    @router.get("/dashboard")
    def dashboard(user: User, date_from: str, date_to: str): return repo.dashboard(user["id"], date_from, date_to)

    @router.get("/analytics")
    def analytics(user: User, date_from: str, date_to: str): return repo.analytics(user["id"], date_from, date_to)

    @router.get("/metrics")
    def metrics(user: User): return repo.metric_dictionary(user["id"])

    @router.get("/export/{entity}.csv")
    def export_csv(entity: Literal["customers", "assets", "orders", "payments"], user: User):
        table = {"customers": "pulse_customers", "assets": "pulse_assets", "orders": "pulse_orders", "payments": "pulse_payments"}[entity]
        order_by = "occurred_at DESC" if entity == "payments" else "created_at DESC"
        rows = repo.list_entity(user["id"], table, order_by, limit=1000)
        output = io.StringIO()
        if rows:
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader(); writer.writerows(rows)
        return Response(output.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="pulse-{entity}.csv"'})

    return router
