// An on-demand real-ledger desk. It never executes bank transfers or polls.
export function minorUnits(value) {
  const text = String(value).trim();
  if (!/^\d+(?:\.\d{1,2})?$/.test(text)) throw new Error("请输入最多两位小数的正金额。");
  const [whole, fraction = ""] = text.split(".");
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(amount) || amount <= 0 || amount > 100000000) throw new Error("金额超出允许范围。");
  return amount;
}

const node = (tag, text = "", className = "") => {
  const element = document.createElement(tag);
  element.textContent = text; element.className = className;
  return element;
};

export function initPulseSettlements({ host, api, isDemo, currency, refresh, onJournal, onOrder }) {
  let generation = 0; let page = 1; let overview = null;
  const money = (amount, code = currency()) => new Intl.NumberFormat("zh-CN", { style: "currency", currency: code }).format(amount / 100);
  const message = node("p", "", "domain-status"); message.setAttribute("role", "status");
  const button = (text, handler) => {
    const result = node("button", text); result.type = "button";
    result.addEventListener("click", handler); return result;
  };
  const input = (name, type, placeholder, value = "") => {
    const result = node("input"); Object.assign(result, { name, type, placeholder, value, required: true });
    result.setAttribute("aria-label", placeholder); return result;
  };
  const select = (name, label, rows) => {
    const result = node("select"); result.name = name; result.required = true; result.setAttribute("aria-label", label);
    result.append(new Option(label, ""));
    for (const [value, text] of rows) result.append(new Option(text, value));
    return result;
  };
  const section = (title, copy) => {
    const result = node("section", "", "pulse-settlement-card"); result.append(node("h3", title));
    if (copy) result.append(node("p", copy)); return result;
  };
  const drawer = (title) => {
    const result = node("details", "", "domain-drawer"); result.append(node("summary", title)); return result;
  };
  const form = (label, fields, command) => {
    const result = node("form", "", "domain-form compact-form");
    const submit = node("button", label, "domain-primary"); submit.type = "submit";
    result.append(...fields, submit);
    let pending = null; let confirmed = false;
    result.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled || confirmed) return;
      const epoch = generation;
      try {
        const data = Object.fromEntries(new FormData(result));
        const next = command(data);
        const signature = JSON.stringify(next);
        // On an uncertain response reuse the SAME command rather than charge
        // again. Changing an uncertain command requires refreshing the ledger.
        if (pending && pending.signature !== signature) throw new Error("上次操作结果尚未确认，请保持原数据重试，或先刷新核对账本。");
        pending ||= { signature, request_id: crypto.randomUUID(), ...next };
        submit.disabled = true;
        message.textContent = "正在入账，请勿重复操作…";
        await api(pending.path, { method: "POST", body: JSON.stringify({ ...pending.body, request_id: pending.request_id }) });
        confirmed = true;
        if (epoch !== generation || isDemo()) return;
        message.textContent = "已入账；正在刷新余额与报表。";
        await refresh();
        if (epoch === generation && !isDemo()) await load();
      } catch (error) {
        if (epoch === generation) message.textContent = confirmed
          ? `已经入账，刷新失败：${error.message}。请点刷新，勿再次登记。`
          : `${error.message}（若响应超时，请保持原数据重试同一请求。）`;
      } finally { if (epoch === generation && !confirmed) submit.disabled = false; }
    });
    return result;
  };
  const accountSelect = () => {
    const result = select("cash_account_code", "实际收付科目", overview.cash_accounts.map((row) => [row.code, `${row.code} · ${row.name}`]));
    result.value = overview.cash_accounts[0]?.code || ""; return result;
  };
  const dueDate = () => {
    const today = new Date();
    const localDay = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
    return input("due_date", "date", "到期日期", localDay);
  };
  const amount = (limit) => {
    const result = input("amount", "text", "本次实际金额"); result.inputMode = "decimal";
    if (limit) result.value = (limit / 100).toFixed(2); return result;
  };

  async function showBill(bill, hostNode) {
    const epoch = generation;
    hostNode.replaceChildren(node("p", "正在读取核销明细…"));
    try {
      const [detail, quote] = await Promise.all([
        api(`/pulse/settlements/bills/${encodeURIComponent(bill.id)}`),
        bill.side === "receivable" ? api(`/pulse/settlements/orders/${encodeURIComponent(bill.source_id)}`) : Promise.resolve(null),
      ]);
      if (epoch !== generation || isDemo()) return;
      hostNode.replaceChildren();
      const current = detail.bill;
      if (quote) {
        hostNode.append(node("p", `订单总额 ${money(quote.rental_total_cents, quote.currency)} · 已结租金 ${money(quote.rental_paid_cents, quote.currency)} · 可用押金 ${money(quote.deposit_available_cents, quote.currency)}`));
        hostNode.append(button("查看来源订单", () => onOrder(bill.source_id)));
      }
      if (current.journal_id) hostNode.append(button("查看应收确认凭证", () => onJournal({ id: current.journal_id })));
      for (const entry of detail.allocations) {
        const label = { receipt: "租金收款", supplier_payment: "供应商付款", deposit_offset: "押金抵租金" }[entry.kind] || entry.kind;
        hostNode.append(button(`${label} ${money(entry.amount_cents, current.currency)} · ${entry.occurred_at.slice(0, 10)} · 查看凭证`, () => onJournal({ id: entry.journal_id })));
        if (entry.reversed_at) {
          hostNode.append(node("p", `已冲销 · ${entry.reversed_at.slice(0, 10)} · ${entry.reversal_reason}`));
          hostNode.append(button("查看冲销凭证", () => onJournal({ id: entry.reversal_journal_id })));
        } else {
          const correction = drawer("更正错误核销（仅冲销账务，不退款）");
          const reason = input("reason", "text", "账务更正原因（至少4字）"); reason.minLength = 4; reason.maxLength = 300;
          correction.append(node("p", "仅用于误录金额/科目等账务差错；真实退租退款不能用此功能代替。原记录保留，余额恢复，并在当前开放期间生成反向凭证。"));
          correction.append(form("确认冲销此笔错误核销", [reason], (data) => ({
            path: `/pulse/settlements/allocations/${encodeURIComponent(entry.id)}/reverse`, body: { reason: data.reason },
          }))); hostNode.append(correction);
        }
      }
      if (detail.has_more) hostNode.append(node("p", "此处展示最近读取的 200 条核销记录。"));
      if (current.remaining_cents <= 0) return;
      const reference = input("reference", "text", "凭证/收付参考号（可选）"); reference.required = false;
      hostNode.append(form(current.side === "receivable" ? "确认已收款并核销" : "确认已付款并核销",
        [amount(current.remaining_cents), accountSelect(), reference], (data) => ({
          path: `/pulse/settlements/bills/${current.id}/clear`,
          body: { amount_cents: minorUnits(data.amount), cash_account_code: data.cash_account_code, reference: data.reference },
        })));
      if (quote && ["returned", "completed"].includes(quote.status) && quote.deposit_available_cents > 0) {
        const limit = Math.min(current.remaining_cents, quote.deposit_available_cents);
        const offset = drawer("使用客户押金抵扣剩余租金（非现金流）");
        offset.append(form("确认押金抵扣", [amount(limit)], (data) => ({
          path: `/pulse/settlements/bills/${current.id}/deposit-offset`, body: { amount_cents: minorUnits(data.amount) },
        }))); hostNode.append(offset);
      }
      hostNode.append(node("p", "仅记录已经发生的实际收付，不会替你转账。误录核销须填写原因生成冲销凭证，不直接改写原记录。"));
    } catch (error) { if (epoch === generation) hostNode.replaceChildren(node("p", error.message)); }
  }

  function render(data) {
    host.replaceChildren(); host.append(message); message.textContent = data.boundary;
    const summary = section("租赁结算与往来核销", "先核对已交付订单的剩余应收、供应商未付费用，再按真实金额分次核销；押金不直接计入收入。");
    for (const row of data.totals) summary.append(node("p", `${row.side === "receivable" ? "应收" : "应付"}余额 ${money(row.outstanding, row.currency)} · 逾期 ${money(row.overdue, row.currency)}`));
    if (!data.totals.length) summary.append(node("p", "暂无结算单。确认应收不会重复计入历史已收租金；登记应付不会再次计入费用。"));
    summary.append(button("刷新结算余额", () => load())); host.append(summary);
    const compose = section("登记结算单", "应收只能从已交付订单确认；应付只能从已登记且明确未付的费用生成。结算单不是税务发票。");
    compose.append(form("确认剩余租金应收", [select("source_id", "选择已交付订单", data.unbilled_orders.map((row) =>
      [row.id, `${row.counterparty} · ${row.id.slice(-8)} · 订单总额 ${money(row.total_cents, row.currency)}`])), dueDate()],
      (body) => ({ path: "/pulse/settlements/bills/receivable", body })));
    compose.append(form("登记供应商应付", [select("source_id", "选择未付费用", data.unbilled_expenses.map((row) =>
      [row.id, `${row.description || row.category} · ${money(row.amount_cents, row.currency)}`])), dueDate()],
      (body) => ({ path: "/pulse/settlements/bills/payable", body })));
    const supplier = drawer("登记供应商及未付费用");
    const contact = input("contact", "text", "联系人（可选）"); contact.required = false;
    supplier.append(form("保存供应商", [input("name", "text", "供应商名称"), contact], (body) => ({ path: "/pulse/settlements/vendors", body })));
    const vendor = select("vendor_id", "供应商（可选）", data.vendors.map((row) => [row.id, row.name])); vendor.required = false;
    supplier.append(form("保存未付费用", [select("category", "费用类型", [["laundry", "洗护"], ["repair", "维修"], ["inventory_purchase", "商品存货采购"], ["delivery", "配送"], ["other", "其他"]]),
      vendor, amount(), input("description", "text", "费用说明/供应商账单号")], (body) => ({
        path: "/pulse/expenses", body: { category: body.category, vendor_id: body.vendor_id || null,
          amount_cents: minorUnits(body.amount), description: body.description, status: "unpaid" },
      })));
    compose.append(supplier); host.append(compose);
    const bills = section("应收 / 应付单", "开放、部分核销与已结清单据按到期日排列；点击展开查看来源凭证及核销记录。");
    for (const bill of data.bills) {
      const title = `${bill.side === "receivable" ? "应收" : "应付"} · ${bill.counterparty} · 未结 ${money(bill.remaining_cents, bill.currency)} · ${bill.status === "settled" ? "已结清" : bill.status === "partial" ? "部分核销" : "待核销"}${bill.overdue ? " · 逾期" : ""}`;
      const item = drawer(title); item.append(node("p", `到期 ${bill.due_date} · 原额 ${money(bill.amount_cents, bill.currency)} · 已核销 ${money(bill.settled_cents, bill.currency)}`));
      const content = node("div", "", "pulse-settlement-detail"); item.append(content);
      item.addEventListener("toggle", () => { if (item.open && !content.dataset.loaded) { content.dataset.loaded = "true"; showBill(bill, content); } });
      bills.append(item);
    }
    if (!data.bills.length) bills.append(node("p", "暂无结算单，不会自动把历史订单批量确认为收入。"));
    const pager = node("div", "", "detail-actions");
    if (page > 1) pager.append(button("上一页", () => { page -= 1; load(); }));
    if (data.has_more) pager.append(button("下一页", () => { page += 1; load(); }));
    bills.append(pager); host.append(bills);
  }

  async function load() {
    const epoch = ++generation;
    if (isDemo()) { host.replaceChildren(node("p", "结算核销只操作真实账本。请切换“真实 Oia”；隔离 Demo 不会读取或改动真实资金记录。")); return; }
    host.replaceChildren(message); message.textContent = "正在读取结算余额…";
    try {
      const data = await api(`/pulse/settlements/overview?page=${page}&page_size=30`);
      if (epoch !== generation || isDemo()) return;
      overview = data; render(data);
    } catch (error) { if (epoch === generation) message.textContent = `无法读取结算数据：${error.message}`; }
  }
  return { load, reset() { generation += 1; page = 1; overview = null; host.replaceChildren(); } };
}
