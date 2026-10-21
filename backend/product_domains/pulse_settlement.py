"""Rental settlement and open-item clearing, not a payment gateway.

Amounts are integer minor units. Every financial command is replay-safe and
commits its source record, allocation, accounting event and balanced journal
in one short transaction. BEGIN IMMEDIATE also takes the production adapter's
cross-process PostgreSQL transaction lock. No external I/O occurs under it.
"""

import hashlib
import json
import uuid
from datetime import date, datetime, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator


def now():
    return datetime.now(timezone.utc).isoformat()


class SettlementConflict(ValueError):
    status_code = 409


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=16, max_length=120)


class BillWrite(Command):
    source_id: str = Field(min_length=1, max_length=120)
    due_date: date


class ClearingWrite(Command):
    amount_cents: int = Field(strict=True, gt=0, le=100_000_000)
    cash_account_code: str = Field(default="1000", min_length=1, max_length=30)
    reference: str = Field(default="", max_length=180)
    occurred_at: datetime | None = None

    @field_validator("occurred_at")
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError("结算时间必须包含时区。")
        return value


class VendorWrite(Command):
    name: str = Field(min_length=1, max_length=180)
    contact: str = Field(default="", max_length=180)

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("供应商名称不能为空。")
        return value.strip()


class ReversalWrite(Command):
    reason: str = Field(min_length=4, max_length=300)

    @field_validator("reason")
    @classmethod
    def meaningful(cls, value):
        if len(value.strip()) < 4:
            raise ValueError("请填写至少4字的账务更正原因。")
        return value.strip()


def init_settlement_schema(connect):
    # Existing private-schema grants/default-privilege protection also applies
    # to these tables. They are accessed only through authenticated app APIs.
    with connect() as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS pulse_settlement_bills (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('receivable','payable')),
                source_type TEXT NOT NULL CHECK(source_type IN ('order','expense')),
                source_id TEXT NOT NULL,amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
                settled_cents INTEGER NOT NULL DEFAULT 0,
                currency TEXT NOT NULL,due_date TEXT NOT NULL,counterparty TEXT NOT NULL,
                journal_id TEXT,created_at TEXT NOT NULL,
                CHECK(settled_cents>=0 AND settled_cents<=amount_cents),
                UNIQUE(user_id,side,source_type,source_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(journal_id) REFERENCES pulse_journals(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_settlement_allocations (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,bill_id TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
                kind TEXT NOT NULL CHECK(kind IN ('receipt','supplier_payment','deposit_offset')),
                payment_id TEXT,journal_id TEXT NOT NULL,reference TEXT NOT NULL DEFAULT '',
                occurred_at TEXT NOT NULL,created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(bill_id) REFERENCES pulse_settlement_bills(id),
                FOREIGN KEY(payment_id) REFERENCES pulse_payments(id),
                FOREIGN KEY(journal_id) REFERENCES pulse_journals(id)
            );
            CREATE TABLE IF NOT EXISTS pulse_finance_commands (
                user_id INTEGER NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
                response_json TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(user_id,request_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pulse_settlement_reversals (
                id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,allocation_id TEXT NOT NULL UNIQUE,
                reason TEXT NOT NULL,original_journal_id TEXT NOT NULL,reversal_journal_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(allocation_id) REFERENCES pulse_settlement_allocations(id),
                FOREIGN KEY(original_journal_id) REFERENCES pulse_journals(id),
                FOREIGN KEY(reversal_journal_id) REFERENCES pulse_journals(id)
            );
            CREATE INDEX IF NOT EXISTS idx_pulse_bills_owner_due
                ON pulse_settlement_bills(user_id,side,due_date,id);
            CREATE INDEX IF NOT EXISTS idx_pulse_allocations_bill
                ON pulse_settlement_allocations(user_id,bill_id,occurred_at);
            CREATE INDEX IF NOT EXISTS idx_pulse_payment_order_kind
                ON pulse_payments(user_id,order_id,payment_type);
        """)


class SettlementRepository:
    def __init__(self, repo):
        self.repo, self.connect = repo, repo.connect

    def _command(self, user_id, operation, payload, execute):
        self.repo.ensure_user_setup(user_id)
        data = payload.model_dump(mode="json")
        request_id = data.pop("request_id", None) or str(uuid.uuid4())
        fingerprint = hashlib.sha256(json.dumps([operation, data], sort_keys=True,
                                               ensure_ascii=False).encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT fingerprint,response_json FROM pulse_finance_commands WHERE user_id=? AND request_id=?",
                (user_id, request_id),
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise SettlementConflict("同一请求编号不能用于不同的结算操作或金额。")
                return json.loads(previous["response_json"])
            result = execute(connection)
            connection.execute(
                """INSERT INTO pulse_finance_commands(user_id,request_id,fingerprint,response_json,created_at)
                   VALUES(?,?,?,?,?)""",
                (user_id, request_id, fingerprint, json.dumps(result, ensure_ascii=False), now()),
            )
            return result

    def _owned(self, connection, table, item_id, user_id):
        return self.repo._owned(connection, table, item_id, user_id)

    def _settings(self, connection, user_id):
        return dict(connection.execute("SELECT * FROM pulse_settings WHERE user_id=?", (user_id,)).fetchone())

    def _cash(self, connection, user_id, code):
        account = self.repo._account(connection, user_id, code=code)
        if account["account_type"] != "ASSET" or account["role"] not in {"cash", "bank"}:
            raise ValueError("收付款科目必须是当前账号的现金或银行资产科目。")
        return account["code"]

    def _code(self, connection, user_id, role):
        return self.repo._account(connection, user_id, role=role)["code"]

    def _journal(self, connection, user_id, *, source_type, source_id, description,
                 occurred_at, debit, credit, amount, cashflow="", order_id=None,
                 asset_id=None, vendor_id=None, event_type=None):
        occurred = occurred_at or datetime.now(timezone.utc)
        if occurred.tzinfo is None:  # Backwards compatibility for existing capture forms.
            occurred = occurred.replace(tzinfo=timezone.utc)
        occurred = occurred.astimezone(timezone.utc)
        posting = occurred.date().isoformat()
        self.repo._period_open(connection, user_id, posting)
        accounts = [self.repo._account(connection, user_id, code=code) for code in (debit, credit)]
        currency = self._settings(connection, user_id)["currency"]
        event_id, journal_id, created = str(uuid.uuid4()), str(uuid.uuid4()), now()
        connection.execute(
            """INSERT INTO pulse_accounting_events
               (id,user_id,event_type,source_document_type,source_document_id,occurred_at,cash_flow_category,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, user_id, event_type or source_type, source_type, source_id, occurred.isoformat(), cashflow, created),
        )
        connection.execute(
            """INSERT INTO pulse_journals
               (id,user_id,accounting_event_id,status,transaction_date,posting_date,description,
                source_document_type,source_document_id,created_by,created_at,posted_at)
               VALUES(?,?,?,'posted',?,?,?,?,?,?,?,?)""",
            (journal_id, user_id, event_id, posting, posting, description, source_type,
             source_id, user_id, created, created),
        )
        lines = []
        for index, account in enumerate(accounts):
            line = {"id": str(uuid.uuid4()), "account_code": account["code"],
                    "account_name": account["name"], "debit_cents": amount if index == 0 else 0,
                    "credit_cents": amount if index == 1 else 0}
            connection.execute(
                """INSERT INTO pulse_journal_lines
                   (id,user_id,journal_id,account_id,debit_cents,credit_cents,currency,
                    order_id,asset_id,vendor_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (line["id"], user_id, journal_id, account["id"], line["debit_cents"],
                 line["credit_cents"], currency, order_id, asset_id, vendor_id),
            )
            lines.append(line)
        return {"id": journal_id, "description": description, "posting_date": posting,
                "source_document_type": source_type, "source_document_id": source_id, "lines": lines,
                "balanced": True, "total_debit": amount, "total_credit": amount, "status": "posted"}

    def _quote(self, connection, user_id, order_id):
        order = self._owned(connection, "pulse_orders", order_id, user_id)
        totals = {row["payment_type"]: int(row["amount"]) for row in connection.execute(
            """SELECT payment_type,SUM(amount_cents) AS amount FROM pulse_payments
               WHERE user_id=? AND order_id=? AND status='recorded' GROUP BY payment_type""",
            (user_id, order_id),
        ).fetchall()}
        paid = totals.get("rental", 0) + totals.get("deposit_offset", 0)
        deposit = totals.get("deposit", 0) - sum(totals.get(key, 0) for key in
                                               ("deposit_refund", "damage_deduction", "deposit_offset"))
        bill = connection.execute(
            "SELECT * FROM pulse_settlement_bills WHERE user_id=? AND source_type='order' AND source_id=?",
            (user_id, order_id),
        ).fetchone()
        return {"order_id": order_id, "status": order["status"], "currency": order["currency"],
                "rental_total_cents": order["total_cents"], "rental_paid_cents": paid,
                "rental_due_cents": max(0, order["total_cents"] - paid),
                "overpayment_cents": max(0, paid - order["total_cents"]),
                "deposit_received_cents": totals.get("deposit", 0),
                "deposit_available_cents": max(0, deposit), "deposit_integrity_ok": deposit >= 0,
                "bill": self._bill_view(bill) if bill else None}

    def quote(self, user_id, order_id):
        with self.connect() as connection:
            return self._quote(connection, user_id, order_id)

    @staticmethod
    def _bill_view(row):
        bill = dict(row)
        bill["remaining_cents"] = bill["amount_cents"] - bill["settled_cents"]
        bill["status"] = "settled" if bill["remaining_cents"] == 0 else "partial" if bill["settled_cents"] else "open"
        bill["overdue"] = bill["remaining_cents"] > 0 and bill["due_date"] < datetime.now(timezone.utc).date().isoformat()
        return bill

    def open_bill(self, user_id, side, payload):
        def execute(connection):
            source_type = "order" if side == "receivable" else "expense"
            source = self._owned(connection, "pulse_orders" if source_type == "order" else "pulse_expenses",
                                 payload.source_id, user_id)
            existing = connection.execute(
                "SELECT * FROM pulse_settlement_bills WHERE user_id=? AND source_type=? AND source_id=?",
                (user_id, source_type, payload.source_id),
            ).fetchone()
            if existing:
                return {"bill": self._bill_view(existing)}
            currency = self._settings(connection, user_id)["currency"]
            if source["currency"] != currency:
                raise ValueError("来源币种与当前账本不一致，不能跨币种核销。")
            bill_id, journal_id = str(uuid.uuid4()), None
            if side == "receivable":
                if source["status"] not in {"rented", "returned", "completed"}:
                    raise ValueError("仅已交付的订单可确认租金应收；预约阶段仍可登记预收款。")
                quote = self._quote(connection, user_id, source["id"])
                amount = quote["rental_due_cents"]
                if amount <= 0:
                    raise ValueError("订单没有剩余租金应收，无需重复开单。")
                customer = self._owned(connection, "pulse_customers", source["customer_id"], user_id)
                counterparty = customer["name"]
                journal = self._journal(connection, user_id, source_type="rental_receivable", source_id=bill_id,
                    description="Confirm remaining delivered rental receivable", occurred_at=None,
                    debit=self._code(connection, user_id, "receivable"),
                    credit=self._code(connection, user_id, "rental_revenue"), amount=amount, order_id=source["id"])
                journal_id = journal["id"]
            else:
                if source["category"] == "asset_loss" or source["status"] != "unpaid":
                    raise ValueError("仅可登记明确未付的费用；历史部分付款金额不明或非现金报损不可生成应付单。")
                amount = source["amount_cents"]
                recognized = connection.execute(
                    """SELECT COALESCE(SUM(l.credit_cents-l.debit_cents),0) AS amount
                       FROM pulse_journals j JOIN pulse_journal_lines l ON l.journal_id=j.id AND l.user_id=j.user_id
                       JOIN pulse_accounts a ON a.id=l.account_id AND a.user_id=j.user_id
                       WHERE j.user_id=? AND j.status='posted' AND j.source_document_type='expense'
                       AND j.source_document_id=? AND a.role='payable'""", (user_id, source["id"]),
                ).fetchone()["amount"]
                if recognized != amount:
                    raise ValueError("原费用的应付分录不完整，请先核对账务，不能核销未确认的债务。")
                counterparty = source["description"] or source["category"]
                if source["vendor_id"]:
                    counterparty = self._owned(connection, "pulse_vendors", source["vendor_id"], user_id)["name"]
                # The expense already debited costs/inventory and credited AP.
                # Register an open item, never recognize that expense twice.
            connection.execute(
                """INSERT INTO pulse_settlement_bills
                   (id,user_id,side,source_type,source_id,amount_cents,currency,due_date,counterparty,journal_id,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (bill_id, user_id, side, source_type, source["id"], amount, currency,
                 payload.due_date.isoformat(), counterparty, journal_id, now()),
            )
            return {"bill": self._bill_view(self._owned(connection, "pulse_settlement_bills", bill_id, user_id))}
        return self._command(user_id, "open_bill:" + side, payload, execute)

    def _allocate(self, connection, user_id, bill, amount, kind, journal_id, payment_id, reference, occurred):
        updated = connection.execute(
            """UPDATE pulse_settlement_bills SET settled_cents=settled_cents+?
               WHERE id=? AND user_id=? AND settled_cents+?<=amount_cents""",
            (amount, bill["id"], user_id, amount),
        )
        if updated.rowcount != 1:
            raise SettlementConflict("核销金额超过当前未结余额，请刷新后重试。")
        allocation_id = str(uuid.uuid4())
        connection.execute(
            """INSERT INTO pulse_settlement_allocations
               (id,user_id,bill_id,amount_cents,kind,payment_id,journal_id,reference,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (allocation_id, user_id, bill["id"], amount, kind, payment_id, journal_id, reference, occurred, now()),
        )
        if bill["side"] == "payable":
            current = self._owned(connection, "pulse_settlement_bills", bill["id"], user_id)
            connection.execute("UPDATE pulse_expenses SET status=? WHERE id=? AND user_id=?",
                ("paid" if current["settled_cents"] == current["amount_cents"] else "partially_paid",
                 bill["source_id"], user_id))
        return allocation_id

    def _payment(self, connection, user_id, *, order_id, kind, amount, occurred_at=None,
                 cash_account_code="1000", method="Manual", reference=""):
        settings = self._settings(connection, user_id)
        quote = self._quote(connection, user_id, order_id) if order_id else None
        if quote and quote["currency"] != settings["currency"]:
            raise ValueError("订单币种与当前账本不一致，不能跨币种结算。")
        if kind in {"deposit", "deposit_refund", "damage_deduction", "deposit_offset"} and not quote:
            raise ValueError("押金必须关联具体订单。")
        if kind in {"deposit_refund", "damage_deduction", "deposit_offset"}:
            if not quote["deposit_integrity_ok"] or amount > quote["deposit_available_cents"]:
                raise SettlementConflict("金额超过当前可用押金，不能超退或重复抵扣。")
        if kind in {"rental", "deposit_offset"} and quote and amount > quote["rental_due_cents"]:
            raise SettlementConflict("金额超过订单剩余租金，不能重复收款或抵扣。")
        if kind == "rental" and quote and quote["status"] == "cancelled":
            raise ValueError("取消订单不能继续收取租金。")
        bill = quote["bill"] if quote else None
        if kind == "deposit_offset" and (not bill or quote["status"] not in {"returned", "completed"}):
            raise ValueError("押金抵租金需先确认应收，并在归还或完成订单后处理。")
        if kind == "damage_deduction":
            evidence = connection.execute(
                """SELECT id FROM pulse_inspections WHERE user_id=? AND order_id=?
                   AND resolution_status='confirmed' AND condition_status IN ('damaged','missing') LIMIT 1""",
                (user_id, order_id),
            ).fetchone()
            if not evidence:
                raise ValueError("损坏扣款必须有已确认的损坏或缺失检查记录。")
        cash = self._cash(connection, user_id, cash_account_code)
        roles = {
            "rental": (cash, self._code(connection, user_id, "receivable" if bill else "rental_revenue")),
            "deposit": (cash, self._code(connection, user_id, "customer_deposit")),
            "deposit_refund": (self._code(connection, user_id, "customer_deposit"), cash),
            "damage_deduction": (self._code(connection, user_id, "customer_deposit"), self._code(connection, user_id, "other_revenue")),
            "deposit_offset": (self._code(connection, user_id, "customer_deposit"), self._code(connection, user_id, "receivable")),
            "product_sale": (cash, self._code(connection, user_id, "product_revenue")),
            "service": (cash, self._code(connection, user_id, "service_revenue")),
        }
        payment_id, created = str(uuid.uuid4()), now()
        occurred = occurred_at or datetime.now(timezone.utc)
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        occurred = occurred.astimezone(timezone.utc)
        if bill and kind in {"rental", "deposit_offset"} and occurred.date().isoformat() < bill["created_at"][:10]:
            raise ValueError("核销日期不能早于应收确认日；此前已收款应先核对历史收款记录。")
        connection.execute(
            """INSERT INTO pulse_payments
               (id,user_id,order_id,payment_type,amount_cents,currency,method,reference,status,occurred_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,'recorded',?,?)""",
            (payment_id, user_id, order_id, kind, amount, settings["currency"], method, reference, occurred.isoformat(), created),
        )
        debit, credit = roles[kind]
        journal = self._journal(connection, user_id, source_type="payment", source_id=payment_id,
            event_type=kind,
            description={"rental": "Rental payment", "deposit": "Customer deposit received",
                         "deposit_refund": "Customer deposit refunded", "damage_deduction": "Confirmed damage deduction",
                         "deposit_offset": "Apply customer deposit to rental receivable",
                         "product_sale": "Product sale receipt", "service": "Service receipt"}[kind],
            occurred_at=occurred, debit=debit, credit=credit, amount=amount,
            cashflow="" if kind in {"deposit_offset", "damage_deduction"} else "operating", order_id=order_id)
        if bill and kind in {"rental", "deposit_offset"}:
            self._allocate(connection, user_id, bill, amount, "deposit_offset" if kind == "deposit_offset" else "receipt",
                           journal["id"], payment_id, reference, occurred.isoformat())
        return {"payment": self._owned(connection, "pulse_payments", payment_id, user_id), "journal": journal}

    def record_payment(self, user_id, payload):
        return self._command(user_id, "payment", payload, lambda connection: self._payment(
            connection, user_id, order_id=payload.order_id, kind=payload.payment_type, amount=payload.amount_cents,
            occurred_at=payload.occurred_at, cash_account_code=payload.cash_account_code,
            method=payload.method, reference=payload.reference))

    def clear_bill(self, user_id, bill_id, payload, *, deposit=False):
        def execute(connection):
            bill = self._owned(connection, "pulse_settlement_bills", bill_id, user_id)
            if bill["currency"] != self._settings(connection, user_id)["currency"]:
                raise ValueError("结算单币种与账本不一致。")
            if deposit and bill["side"] != "receivable":
                raise ValueError("客户押金不能用于支付供应商欠款。")
            if payload.amount_cents > bill["amount_cents"] - bill["settled_cents"]:
                raise SettlementConflict("核销金额超过未结余额。")
            if bill["side"] == "receivable":
                result = self._payment(connection, user_id, order_id=bill["source_id"],
                    kind="deposit_offset" if deposit else "rental", amount=payload.amount_cents,
                    occurred_at=payload.occurred_at, cash_account_code=payload.cash_account_code,
                    reference=payload.reference)
            else:
                expense = self._owned(connection, "pulse_expenses", bill["source_id"], user_id)
                occurred = (payload.occurred_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
                if occurred.date().isoformat() < expense["occurred_at"][:10]:
                    raise ValueError("应付核销日期不能早于费用确认日；供应商预付款不属于本流程。")
                allocation_source = str(uuid.uuid4())
                journal = self._journal(connection, user_id, source_type="supplier_payment", source_id=allocation_source,
                    description="Clear supplier payable", occurred_at=occurred,
                    debit=self._code(connection, user_id, "payable"),
                    credit=self._cash(connection, user_id, payload.cash_account_code), amount=payload.amount_cents,
                    cashflow="operating", order_id=expense["order_id"], asset_id=expense["asset_id"], vendor_id=expense["vendor_id"])
                allocation_id = self._allocate(connection, user_id, bill, payload.amount_cents, "supplier_payment",
                    journal["id"], None, payload.reference, occurred.isoformat())
                result = {"allocation_id": allocation_id, "journal": journal}
            result["bill"] = self._bill_view(self._owned(connection, "pulse_settlement_bills", bill_id, user_id))
            return result
        return self._command(user_id, f"clear:{bill_id}:{deposit}", payload, execute)

    def record_expense(self, user_id, payload):
        def execute(connection):
            if payload.status == "partially_paid":
                raise ValueError("请先登记未付费用，再按实付金额分次核销；不能只标记部分付款而不记录金额。")
            asset = self._owned(connection, "pulse_assets", payload.asset_id, user_id) if payload.asset_id else None
            if payload.order_id:
                self._owned(connection, "pulse_orders", payload.order_id, user_id)
            if payload.vendor_id:
                self._owned(connection, "pulse_vendors", payload.vendor_id, user_id)
            if payload.category == "asset_loss" and (asset is None or asset["accounting_class"] not in {"rental_asset", "inventory"}):
                raise ValueError("资产报损必须选择已分类资产并录入账面净值。")
            item_id, created = str(uuid.uuid4()), now()
            occurred = payload.occurred_at or datetime.now(timezone.utc)
            if occurred.tzinfo is None:
                occurred = occurred.replace(tzinfo=timezone.utc)
            occurred = occurred.astimezone(timezone.utc)
            currency = self._settings(connection, user_id)["currency"]
            connection.execute(
                """INSERT INTO pulse_expenses
                   (id,user_id,vendor_id,order_id,asset_id,category,amount_cents,currency,status,description,occurred_at,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, user_id, payload.vendor_id, payload.order_id, payload.asset_id, payload.category,
                 payload.amount_cents, currency, payload.status, payload.description.strip(), occurred.isoformat(), created),
            )
            if asset:
                connection.execute(
                    """INSERT INTO pulse_asset_events(id,user_id,asset_id,event_type,order_id,amount_cents,notes,occurred_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (str(uuid.uuid4()), user_id, asset["id"], payload.category, payload.order_id,
                     payload.amount_cents, payload.description.strip(), occurred.isoformat(), created),
                )
            roles = {"inventory_purchase": "inventory", "cleaning": "cleaning_expense", "laundry": "cleaning_expense",
                     "repair": "repair_expense", "asset_loss": "other_expense", "delivery": "delivery_expense",
                     "payment_fee": "payment_fee", "marketing": "marketing_expense", "rent": "rent_expense",
                     "utilities": "utilities_expense", "other": "other_expense"}
            credit = (self._code(connection, user_id, "rental_asset" if asset["accounting_class"] == "rental_asset" else "inventory")
                      if payload.category == "asset_loss" else self._cash(connection, user_id, payload.cash_account_code)
                      if payload.status == "paid" else self._code(connection, user_id, "payable"))
            journal = self._journal(connection, user_id, source_type="asset_loss" if payload.category == "asset_loss" else "expense",
                source_id=item_id, description=f"{payload.category} expense", occurred_at=occurred,
                debit=self._code(connection, user_id, roles[payload.category]), credit=credit, amount=payload.amount_cents,
                cashflow="operating" if payload.status == "paid" and payload.category != "asset_loss" else "",
                order_id=payload.order_id, asset_id=payload.asset_id, vendor_id=payload.vendor_id)
            return {"expense": self._owned(connection, "pulse_expenses", item_id, user_id), "journal": journal}
        return self._command(user_id, "expense", payload, execute)

    def overview(self, user_id, page=1, page_size=30):
        self.repo.ensure_user_setup(user_id)
        with self.connect() as connection:
            totals = connection.execute(
                """SELECT side,currency,SUM(amount_cents-settled_cents) AS outstanding,
                   SUM(CASE WHEN due_date<? THEN amount_cents-settled_cents ELSE 0 END) AS overdue
                   FROM pulse_settlement_bills WHERE user_id=? GROUP BY side,currency""",
                (datetime.now(timezone.utc).date().isoformat(), user_id),
            ).fetchall()
            rows = connection.execute(
                """SELECT * FROM pulse_settlement_bills WHERE user_id=?
                   ORDER BY CASE WHEN settled_cents<amount_cents THEN 0 ELSE 1 END,due_date,id LIMIT ? OFFSET ?""",
                (user_id, page_size + 1, (page - 1) * page_size),
            ).fetchall()
            orders = connection.execute(
                """SELECT o.id,o.total_cents,o.currency,c.name AS counterparty FROM pulse_orders o
                   JOIN pulse_customers c ON c.id=o.customer_id AND c.user_id=o.user_id
                   WHERE o.user_id=? AND o.status IN ('rented','returned','completed')
                   AND NOT EXISTS(SELECT 1 FROM pulse_settlement_bills b WHERE b.user_id=o.user_id
                                  AND b.source_type='order' AND b.source_id=o.id)
                   AND o.total_cents>COALESCE((SELECT SUM(p.amount_cents) FROM pulse_payments p
                       WHERE p.user_id=o.user_id AND p.order_id=o.id AND p.status='recorded'
                       AND p.payment_type IN ('rental','deposit_offset')),0)
                   ORDER BY o.created_at DESC LIMIT 100""", (user_id,),
            ).fetchall()
            expenses = connection.execute(
                """SELECT e.id,e.category,e.description,e.amount_cents,e.currency FROM pulse_expenses e
                   WHERE e.user_id=? AND e.status='unpaid' AND e.category!='asset_loss'
                   AND NOT EXISTS(SELECT 1 FROM pulse_settlement_bills b WHERE b.user_id=e.user_id
                                  AND b.source_type='expense' AND b.source_id=e.id)
                   ORDER BY e.occurred_at DESC LIMIT 100""", (user_id,),
            ).fetchall()
            accounts = connection.execute(
                "SELECT code,name FROM pulse_accounts WHERE user_id=? AND active=1 AND account_type='ASSET' AND role IN ('cash','bank') ORDER BY code",
                (user_id,),
            ).fetchall()
            vendors = connection.execute("SELECT id,name FROM pulse_vendors WHERE user_id=? ORDER BY name,id LIMIT 200", (user_id,)).fetchall()
        return {"totals": [dict(row) for row in totals], "bills": [self._bill_view(row) for row in rows[:page_size]],
                "page": page, "has_more": len(rows) > page_size, "unbilled_orders": [dict(row) for row in orders],
                "unbilled_expenses": [dict(row) for row in expenses], "cash_accounts": [dict(row) for row in accounts],
                "vendors": [dict(row) for row in vendors],
                "boundary": "仅记录已实际发生的收付及账务核销，不执行银行转账；银行流水导入对账暂不包含。"}

    def create_vendor(self, user_id, payload):
        def execute(connection):
            item_id, created = str(uuid.uuid4()), now()
            connection.execute(
                "INSERT INTO pulse_vendors(id,user_id,name,contact,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (item_id, user_id, payload.name, payload.contact.strip(), created, created),
            )
            return self._owned(connection, "pulse_vendors", item_id, user_id)
        return self._command(user_id, "vendor", payload, execute)

    def reverse_allocation(self, user_id, allocation_id, payload):
        """Correct erroneous bookkeeping; never issue a refund or erase evidence."""
        def execute(connection):
            allocation = self._owned(connection, "pulse_settlement_allocations", allocation_id, user_id)
            prior = connection.execute(
                "SELECT id FROM pulse_settlement_reversals WHERE user_id=? AND allocation_id=?",
                (user_id, allocation_id),
            ).fetchone()
            if prior:
                raise SettlementConflict("这笔核销已经冲销，不能重复冲销；请刷新账本。")
            bill = self._owned(connection, "pulse_settlement_bills", allocation["bill_id"], user_id)
            original = self._owned(connection, "pulse_journals", allocation["journal_id"], user_id)
            if original["status"] != "posted" or original["reversed_by"]:
                raise SettlementConflict("原凭证不可冲销或已被冲销，请先核对账务。")
            self.repo._period_open(connection, user_id, original["posting_date"])
            if original["posting_date"] > datetime.now(timezone.utc).date().isoformat():
                raise ValueError("不能提前冲销未来日期的核销凭证。")
            lines = connection.execute(
                """SELECT l.*,a.code FROM pulse_journal_lines l
                   JOIN pulse_accounts a ON a.id=l.account_id AND a.user_id=l.user_id
                   WHERE l.user_id=? AND l.journal_id=? ORDER BY l.debit_cents DESC""",
                (user_id, original["id"]),
            ).fetchall()
            amount = allocation["amount_cents"]
            if (len(lines) != 2 or lines[0]["debit_cents"] != amount or lines[0]["credit_cents"] != 0
                    or lines[1]["credit_cents"] != amount or lines[1]["debit_cents"] != 0
                    or any(line["currency"] != bill["currency"] for line in lines)):
                raise ValueError("原核销凭证与金额不一致，不能自动生成冲销。")
            reversal_id = str(uuid.uuid4())
            event = self._owned(connection, "pulse_accounting_events", original["accounting_event_id"], user_id)
            journal = self._journal(connection, user_id, source_type="settlement_reversal", source_id=reversal_id,
                description="核销账务更正：" + payload.reason, occurred_at=None,
                debit=lines[1]["code"], credit=lines[0]["code"], amount=amount,
                cashflow=event["cash_flow_category"], order_id=lines[0]["order_id"],
                asset_id=lines[0]["asset_id"], vendor_id=lines[0]["vendor_id"])
            connection.execute("UPDATE pulse_journals SET reversed_by=? WHERE user_id=? AND id=?",
                               (journal["id"], user_id, original["id"]))
            connection.execute("UPDATE pulse_journals SET reversal_of=? WHERE user_id=? AND id=?",
                               (original["id"], user_id, journal["id"]))
            journal["reversal_of"] = original["id"]
            changed = connection.execute(
                "UPDATE pulse_settlement_bills SET settled_cents=settled_cents-? WHERE user_id=? AND id=? AND settled_cents>=?",
                (amount, user_id, bill["id"], amount))
            if changed.rowcount != 1:
                raise SettlementConflict("单据余额与核销记录不一致，冲销未入账。")
            if allocation["payment_id"]:
                changed = connection.execute("UPDATE pulse_payments SET status='reversed' WHERE user_id=? AND id=? AND status='recorded'",
                                             (user_id, allocation["payment_id"]))
                if changed.rowcount != 1:
                    raise SettlementConflict("原收款或押金抵扣已变更，冲销未入账。")
            updated_bill = self._bill_view(self._owned(connection, "pulse_settlement_bills", bill["id"], user_id))
            if bill["side"] == "payable":
                connection.execute("UPDATE pulse_expenses SET status=? WHERE user_id=? AND id=?",
                    ("partially_paid" if updated_bill["settled_cents"] else "unpaid", user_id, bill["source_id"]))
            connection.execute(
                """INSERT INTO pulse_settlement_reversals
                   (id,user_id,allocation_id,reason,original_journal_id,reversal_journal_id,created_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (reversal_id, user_id, allocation_id, payload.reason, original["id"], journal["id"], now()))
            return {"bill": updated_bill, "journal": journal, "reversal_id": reversal_id,
                    "boundary": "仅冲销错误账务；原单据保留，不执行退款或银行转账。"}
        return self._command(user_id, "reverse_allocation:" + allocation_id, payload, execute)

    def bill_detail(self, user_id, bill_id):
        with self.connect() as connection:
            bill = self._bill_view(self._owned(connection, "pulse_settlement_bills", bill_id, user_id))
            allocations = connection.execute(
                """SELECT a.*,r.created_at AS reversed_at,r.reason AS reversal_reason,
                   r.reversal_journal_id FROM pulse_settlement_allocations a
                   LEFT JOIN pulse_settlement_reversals r ON r.allocation_id=a.id AND r.user_id=a.user_id
                   WHERE a.user_id=? AND a.bill_id=? ORDER BY a.occurred_at DESC,a.id LIMIT 201""",
                (user_id, bill_id),
            ).fetchall()
        return {"bill": bill, "allocations": [dict(row) for row in allocations[:200]], "has_more": len(allocations) > 200}


def create_settlement_router(repo, current_user):
    router = APIRouter(prefix="/settlements", tags=["脉冲域结算"])
    service = SettlementRepository(repo)
    User = Annotated[dict, Depends(current_user)]

    def safe(call):
        try:
            return call()
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'")) from exc
        except ValueError as exc:
            raise HTTPException(getattr(exc, "status_code", 400), str(exc)) from exc

    @router.get("/overview")
    def overview(user: User, page: int = Query(1, ge=1, le=10000), page_size: int = Query(30, ge=1, le=100)):
        return safe(lambda: service.overview(user["id"], page, page_size))

    @router.get("/orders/{order_id}")
    def quote(order_id: str, user: User):
        return safe(lambda: service.quote(user["id"], order_id))

    @router.post("/vendors", status_code=201)
    def vendor(payload: VendorWrite, user: User):
        return safe(lambda: service.create_vendor(user["id"], payload))

    @router.post("/bills/{side}", status_code=201)
    def open_bill(side: Literal["receivable", "payable"], payload: BillWrite, user: User):
        return safe(lambda: service.open_bill(user["id"], side, payload))

    @router.get("/bills/{bill_id}")
    def detail(bill_id: str, user: User):
        return safe(lambda: service.bill_detail(user["id"], bill_id))

    @router.post("/bills/{bill_id}/clear")
    def clear(bill_id: str, payload: ClearingWrite, user: User):
        return safe(lambda: service.clear_bill(user["id"], bill_id, payload))

    @router.post("/bills/{bill_id}/deposit-offset")
    def offset(bill_id: str, payload: ClearingWrite, user: User):
        return safe(lambda: service.clear_bill(user["id"], bill_id, payload, deposit=True))

    @router.post("/allocations/{allocation_id}/reverse")
    def reverse(allocation_id: str, payload: ReversalWrite, user: User):
        return safe(lambda: service.reverse_allocation(user["id"], allocation_id, payload))

    return router
