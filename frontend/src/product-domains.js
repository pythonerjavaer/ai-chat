const $ = (id) => document.getElementById(id);

const escapeMoney = (cents, currency = "AUD") => new Intl.NumberFormat("zh-CN", {
  style: "currency", currency, maximumFractionDigits: 2,
}).format((Number(cents) || 0) / 100);

const dateValue = (date) => date.toISOString().slice(0, 10);
const isoInput = (value) => value ? new Date(value).toISOString() : null;

function option(value, label) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  return node;
}

function empty(text) {
  const node = document.createElement("div");
  node.className = "domain-empty";
  node.textContent = text;
  return node;
}

function card(title, meta = "", body = "") {
  const node = document.createElement("article");
  node.className = "domain-record-card";
  const heading = document.createElement("div");
  const strong = document.createElement("strong");
  const small = document.createElement("small");
  strong.textContent = title;
  small.textContent = meta;
  heading.append(strong, small);
  const copy = document.createElement("p");
  copy.textContent = body;
  node.append(heading, copy);
  return node;
}

function renderList(host, items, render, emptyText) {
  host.replaceChildren();
  if (!items.length) return host.appendChild(empty(emptyText));
  items.forEach((item) => host.appendChild(render(item)));
}

export function initProductDomains({ api, toast }) {
  const leapDialog = $("leap-domain-dialog");
  const pulseDialog = $("pulse-domain-dialog");
  const leap = { materials: [], excerpts: [], notes: [], wormholes: [], clashes: [], activeMaterial: null };
  const pulse = { currency: "AUD", customers: [], skus: [], assets: [], orders: [], payments: [], inspections: [], expenses: [] };

  function setStatus(id, text, tone = "") {
    const target = $(id);
    if (!target) return;
    target.textContent = text;
    target.dataset.tone = tone;
  }

  function selectOptions(select, items, label, blank = "请选择") {
    if (!select) return;
    select.replaceChildren(option("", blank));
    items.forEach((item) => select.appendChild(option(item.id, label(item))));
  }

  function activate(dialog, name) {
    dialog.querySelectorAll("[data-domain-tab]").forEach((button) => button.classList.toggle("active", button.dataset.domainTab === name));
    dialog.querySelectorAll("[data-domain-panel]").forEach((panel) => panel.hidden = panel.dataset.domainPanel !== name);
  }

  async function loadLeapBase() {
    setStatus("leap-status", "正在读取你的材料与证据…");
    const [materials, excerpts, notes, wormholes, clashes] = await Promise.all([
      api("/leap/materials?limit=100"), api("/leap/excerpts"), api("/leap/notes"), api("/leap/wormholes"), api("/leap/clashes"),
    ]);
    leap.materials = materials.items || [];
    leap.excerpts = excerpts;
    leap.notes = notes;
    leap.wormholes = wormholes;
    leap.clashes = clashes;
    renderLeap();
    setStatus("leap-status", `${leap.materials.length} 份材料 · ${leap.excerpts.length} 条摘录 · ${leap.wormholes.length} 条思想连接`, "ok");
  }

  function renderLeap() {
    renderList($("leap-material-list"), leap.materials, (item) => {
      const node = card(item.title, `${item.author || "作者未知"} · ${item.progress_percent}%`, `${item.paragraph_count} 段 · ${item.tags.join(" / ") || "未添加标签"}`);
      node.tabIndex = 0;
      node.addEventListener("click", () => openMaterial(item.id));
      return node;
    }, "还没有材料。粘贴文本或导入 TXT / Markdown 后，内容会安全保存在当前账号。 ");
    renderList($("leap-excerpt-list"), leap.excerpts, (item) => card(item.material_title, `段落 ${item.paragraph_position + 1}${item.reference_stale ? " · 引用版本已变化" : ""}`, item.quote), "还没有摘录。打开材料，选择一段原文保存证据。 ");
    renderList($("leap-note-list"), leap.notes, (item) => card(item.topic || "未命名笔记", item.material_title || "独立笔记", item.content), "还没有笔记。笔记与原文摘录分开保存。 ");
    renderList($("leap-wormhole-list"), leap.wormholes, (item) => card(`${item.left_material} ⇌ ${item.right_material}`, item.relation_type, `${item.left_quote}\n↔\n${item.right_quote}\n\n${item.reflection}`), "还没有思想虫洞。至少保存两条摘录后，可以建立双侧证据连接。 ");
    renderList($("leap-clash-list"), leap.clashes, (item) => card(item.title, "双观点对照", `A · ${item.viewpoint_a}\nB · ${item.viewpoint_b}\n分歧 · ${item.disagreement || "未填写"}\n判断 · ${item.judgment || "未填写"}`), "还没有思想对撞卡。这里保存观点、证据、分歧和你的判断。 ");
    [$("leap-note-excerpt"), $("leap-wormhole-left"), $("leap-wormhole-right"), $("leap-clash-a"), $("leap-clash-b")].forEach((select) => {
      selectOptions(select, leap.excerpts, (item) => `${item.material_title} · ${item.quote.slice(0, 38)}`);
    });
    selectOptions($("leap-note-material"), leap.materials, (item) => item.title, "可选材料");
  }

  async function openMaterial(id) {
    const result = await api(`/leap/materials/${id}/paragraphs?offset=0&limit=100`);
    leap.activeMaterial = result.material;
    $("leap-reader-title").textContent = result.material.title;
    $("leap-reader-meta").textContent = `${result.material.author || "作者未知"} · ${result.material.paragraph_count} 段 · 已读 ${result.material.progress_percent}%`;
    const host = $("leap-reader-pages");
    host.replaceChildren();
    result.paragraphs.forEach((paragraph) => {
      const node = card(`段落 ${paragraph.position + 1}`, "点击设为当前证据", paragraph.content);
      node.addEventListener("click", () => {
        $("leap-excerpt-position").value = paragraph.position;
        $("leap-excerpt-quote").value = paragraph.content;
        host.querySelectorAll(".selected").forEach((item) => item.classList.remove("selected"));
        node.classList.add("selected");
      });
      host.appendChild(node);
    });
    activate(leapDialog, "reader");
  }

  async function leapSubmit(form, endpoint, success) {
    const data = Object.fromEntries(new FormData(form));
    ["material_id", "excerpt_id"].forEach((key) => {
      if (key in data && data[key] === "") data[key] = null;
    });
    await api(endpoint, { method: "POST", body: JSON.stringify(data) });
    form.reset();
    await loadLeapBase();
    toast(success);
  }

  $("leap-material-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      data.tags = data.tags.split(",").map((value) => value.trim()).filter(Boolean);
      await api("/leap/materials", { method: "POST", body: JSON.stringify(data) });
      event.currentTarget.reset(); await loadLeapBase(); toast("材料已保存到跃迁域。");
    } catch (error) { setStatus("leap-status", error.message, "error"); }
  });

  $("leap-file").addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    try {
      const data = new FormData(); data.append("file", file);
      await api("/leap/materials/import", { method: "POST", body: data, timeoutMs: 30000 });
      event.target.value = ""; await loadLeapBase(); toast("文件已导入跃迁域。");
    } catch (error) { setStatus("leap-status", error.message, "error"); }
  });

  $("leap-progress").addEventListener("click", async () => {
    if (!leap.activeMaterial) return;
    const position = Number($("leap-excerpt-position").value || 0);
    await api(`/leap/materials/${leap.activeMaterial.id}/progress`, { method: "PUT", body: JSON.stringify({ paragraph_position: position, character_offset: 0 }) });
    await loadLeapBase(); toast("阅读位置已保存。");
  });

  $("leap-excerpt-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!leap.activeMaterial) return setStatus("leap-status", "请先打开一份材料。", "error");
    try {
      const quote = $("leap-excerpt-quote").value.trim();
      await api("/leap/excerpts", { method: "POST", body: JSON.stringify({ material_id: leap.activeMaterial.id, material_version: leap.activeMaterial.version, paragraph_position: Number($("leap-excerpt-position").value), start_offset: 0, end_offset: quote.length, quote }) });
      await loadLeapBase(); toast("原文摘录已保存。");
    } catch (error) { setStatus("leap-status", error.message, "error"); }
  });

  $("leap-note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await leapSubmit(event.currentTarget, "/leap/notes", "笔记已保存。"); } catch (error) { setStatus("leap-status", error.message, "error"); }
  });
  $("leap-wormhole-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await leapSubmit(event.currentTarget, "/leap/wormholes", "思想虫洞已建立。"); } catch (error) { setStatus("leap-status", error.message, "error"); }
  });
  $("leap-clash-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      if (!data.evidence_a_excerpt_id) data.evidence_a_excerpt_id = null;
      if (!data.evidence_b_excerpt_id) data.evidence_b_excerpt_id = null;
      await api("/leap/clashes", { method: "POST", body: JSON.stringify(data) });
      event.currentTarget.reset(); await loadLeapBase(); toast("思想对撞卡已保存。");
    } catch (error) { setStatus("leap-status", error.message, "error"); }
  });

  async function loadPulse() {
    const today = new Date();
    const dateFrom = dateValue(new Date(today.getFullYear(), today.getMonth(), 1));
    const dateTo = dateValue(new Date(today.getFullYear(), today.getMonth() + 1, 0));
    setStatus("pulse-status", "正在读取 Oia 业务账本…");
    const [settings, dashboard, customers, skus, assets, orders, payments, inspections, expenses] = await Promise.all([
      api("/pulse/settings"), api(`/pulse/dashboard?date_from=${dateFrom}&date_to=${dateTo}`), api("/pulse/customers"), api("/pulse/skus"), api("/pulse/assets"), api("/pulse/orders"), api("/pulse/payments"), api("/pulse/inspections"), api("/pulse/expenses"),
    ]);
    Object.assign(pulse, { currency: settings.currency, dashboard, customers, skus, assets, orders, payments, inspections, expenses, dateFrom, dateTo });
    renderPulse();
    setStatus("pulse-status", `${customers.length} 位客户 · ${orders.length} 笔订单 · ${assets.length} 件实物资产`, "ok");
  }

  function metric(label, value, hint) {
    const node = document.createElement("article");
    node.className = "pulse-metric";
    const small = document.createElement("small"); const strong = document.createElement("strong"); const p = document.createElement("p");
    small.textContent = label; strong.textContent = value; p.textContent = hint; node.append(small, strong, p); return node;
  }

  function renderPulse() {
    const m = pulse.dashboard.metrics;
    const metrics = [
      ["本月收入", escapeMoney(m.revenue, pulse.currency), "已过账收入，不含押金"], ["订单", m.orders, "当前月创建订单"],
      ["现金流入", escapeMoney(m.cash_in, pulse.currency), "租金、销售、服务与押金实收"], ["经营利润", escapeMoney(m.operating_profit, pulse.currency), pulse.dashboard.data_quality.profitability === "complete" ? "已记录口径" : "部分成本尚未登记"],
      ["持有押金", escapeMoney(m.deposits_held, pulse.currency), "负债，不计入收入"], ["资产可用率", `${m.inventory_availability}%`, "当前可用实物资产 / 全部资产"],
      ["资产占用率", `${m.asset_utilization_proxy}%`, "当前已预约或出租资产"], ["复购客户", `${m.repeat_customer_rate}%`, "两笔及以上有效订单客户"],
    ];
    $("pulse-metrics").replaceChildren(...metrics.map((item) => metric(...item)));
    renderList($("pulse-customer-list"), pulse.customers, (item) => card(item.name, `${item.phone || "无电话"} · ${item.source || "来源未记"}`, item.email || item.notes || "暂无备注"), "还没有客户。先建立客户，订单才会有清晰归属。 ");
    renderList($("pulse-sku-list"), pulse.skus, (item) => card(item.name, `${item.category || "未分类"} · ${item.size || "未标尺寸"}`, `当前标价 ${escapeMoney(item.current_price_cents, pulse.currency)} · 历史订单保留成交快照`), "还没有SKU。SKU表示款式，实物资产表示具体某一件礼服。 ");
    renderList($("pulse-asset-list"), pulse.assets, (item) => {
      const node = card(item.asset_code, `${item.status} · ${item.accounting_class}`, pulse.skus.find((sku) => sku.id === item.sku_id)?.name || "未知SKU");
      const actions = document.createElement("div"); actions.className = "domain-card-actions";
      ["rented", "returned", "inspection", "cleaning", "maintenance", "available"].forEach((state) => {
        const button = document.createElement("button"); button.type = "button"; button.textContent = state;
        button.addEventListener("click", () => transitionAsset(item.id, state)); actions.appendChild(button);
      });
      node.appendChild(actions); return node;
    }, "还没有实物资产。每件可重复出租的礼服都应有独立资产编号。 ");
    renderList($("pulse-order-list"), pulse.orders, (item) => card(`#${item.id.slice(0, 8)}`, `${item.status} · ${item.start_at.slice(0, 10)} → ${item.end_at.slice(0, 10)}`, `${escapeMoney(item.total_cents, item.currency)} · 价格已冻结为成交快照`), "还没有订单。重叠预约会在服务端事务中被拒绝。 ");
    renderList($("pulse-payment-list"), pulse.payments, (item) => card(item.payment_type, item.occurred_at.slice(0, 10), `${escapeMoney(item.amount_cents, item.currency)} · ${item.reference || item.method || "无外部参考"}`), "还没有资金记录。租金、押金、押金退还和确认扣款分开登记。 ");
    renderList($("pulse-inspection-list"), pulse.inspections, (item) => card(item.condition_status, item.resolution_status, item.damage_notes || item.missing_items || "无异常备注"), "还没有归还检查记录。 ");
    fillPulseSelects();
  }

  function fillPulseSelects() {
    selectOptions($("pulse-asset-sku"), pulse.skus, (x) => `${x.name} · ${escapeMoney(x.current_price_cents, pulse.currency)}`);
    selectOptions($("pulse-order-customer"), pulse.customers, (x) => x.name);
    selectOptions($("pulse-order-sku"), pulse.skus, (x) => x.name);
    selectOptions($("pulse-order-asset"), pulse.assets, (x) => `${x.asset_code} · ${x.status}`, "可选具体资产");
    selectOptions($("pulse-payment-order"), pulse.orders, (x) => `#${x.id.slice(0, 8)} · ${escapeMoney(x.total_cents, x.currency)}`, "可选订单");
    selectOptions($("pulse-inspection-order"), pulse.orders, (x) => `#${x.id.slice(0, 8)}`);
    selectOptions($("pulse-inspection-asset"), pulse.assets, (x) => x.asset_code);
    selectOptions($("pulse-expense-asset"), pulse.assets, (x) => x.asset_code, "可选资产");
  }

  async function transitionAsset(id, status) {
    try { await api(`/pulse/assets/${id}/status`, { method: "POST", body: JSON.stringify({ status, notes: "脉冲域人工状态流转" }) }); await loadPulse(); toast(`资产状态已更新为 ${status}。`); }
    catch (error) { setStatus("pulse-status", error.message, "error"); }
  }

  async function submitPulse(form, endpoint, transform, message) {
    const data = Object.fromEntries(new FormData(form));
    transform?.(data);
    ["order_id", "asset_id", "vendor_id", "acquisition_date", "useful_life_months", "purchase_cost_cents"].forEach((key) => {
      if (key in data && data[key] === "") data[key] = null;
    });
    await api(endpoint, { method: "POST", body: JSON.stringify(data) });
    form.reset(); await loadPulse(); toast(message);
  }

  const pulseForms = [
    ["pulse-customer-form", "/pulse/customers", null, "客户已保存。"],
    ["pulse-sku-form", "/pulse/skus", (d) => { d.current_price_cents = Math.round(Number(d.current_price) * 100); delete d.current_price; }, "SKU已保存。"],
    ["pulse-asset-form", "/pulse/assets", (d) => { d.purchase_cost_cents = d.purchase_cost ? Math.round(Number(d.purchase_cost) * 100) : null; delete d.purchase_cost; d.residual_value_cents = 0; d.useful_life_months = d.useful_life_months ? Number(d.useful_life_months) : null; }, "实物资产已保存。"],
    ["pulse-payment-form", "/pulse/payments", (d) => { d.amount_cents = Math.round(Number(d.amount) * 100); delete d.amount; }, "资金记录和会计凭证已保存。"],
    ["pulse-inspection-form", "/pulse/inspections", null, "检查结果已保存。"],
    ["pulse-expense-form", "/pulse/expenses", (d) => { d.amount_cents = Math.round(Number(d.amount) * 100); delete d.amount; }, "费用和会计凭证已保存。"],
  ];
  pulseForms.forEach(([id, endpoint, transform, message]) => $(id).addEventListener("submit", async (event) => {
    event.preventDefault();
    try { await submitPulse(event.currentTarget, endpoint, transform, message); }
    catch (error) { setStatus("pulse-status", error.message, "error"); }
  }));

  $("pulse-order-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget));
      const payload = { customer_id: data.customer_id, start_at: isoInput(data.start_at), end_at: isoInput(data.end_at), channel: data.channel || "", delivery_method: data.delivery_method || "", discount_cents: Math.round(Number(data.discount || 0) * 100), notes: data.notes || "", items: [{ sku_id: data.sku_id, asset_id: data.asset_id || null, quantity: 1, unit_price_cents: data.unit_price ? Math.round(Number(data.unit_price) * 100) : null }] };
      await api("/pulse/orders", { method: "POST", body: JSON.stringify(payload) });
      event.currentTarget.reset(); await loadPulse(); toast("预约订单已创建，成交价格已冻结。");
    } catch (error) { setStatus("pulse-status", error.message, "error"); }
  });

  async function loadFinance() {
    try {
      const [trial, statements, accounts, ledger] = await Promise.all([
        api(`/pulse/trial-balance?date_from=${pulse.dateFrom}&date_to=${pulse.dateTo}`), api(`/pulse/statements?date_from=${pulse.dateFrom}&date_to=${pulse.dateTo}`), api("/pulse/accounts"), api(`/pulse/general-ledger?date_from=${pulse.dateFrom}&date_to=${pulse.dateTo}`),
      ]);
      const host = $("pulse-finance-output"); host.replaceChildren();
      host.append(metric("试算平衡", trial.balanced ? "借贷平衡" : "Accounting Integrity Error", `借方 ${escapeMoney(trial.total_debit, pulse.currency)} · 贷方 ${escapeMoney(trial.total_credit, pulse.currency)}`));
      host.append(metric("营业收入", escapeMoney(statements.income_statement.revenue_total, pulse.currency), statements.income_statement.note));
      host.append(metric("经营利润", escapeMoney(statements.income_statement.operating_profit, pulse.currency), statements.income_statement.data_quality));
      host.append(metric("资产负债表", statements.balance_sheet.balanced ? "平衡" : "期初余额不完整", `${escapeMoney(statements.balance_sheet.assets_total, pulse.currency)} = ${escapeMoney(statements.balance_sheet.liabilities_and_equity_total, pulse.currency)}`));
      renderList($("pulse-account-list"), accounts, (item) => card(`${item.code} · ${item.name}`, item.account_type, item.role || "可配置科目"), "暂无会计科目。");
      renderList($("pulse-ledger-list"), ledger.accounts.filter((item) => item.debit || item.credit || item.opening_balance), (item) => card(`${item.code} · ${item.name}`, item.account_type, `期初 ${escapeMoney(item.opening_balance, pulse.currency)} · 借 ${escapeMoney(item.debit, pulse.currency)} · 贷 ${escapeMoney(item.credit, pulse.currency)} · 期末 ${escapeMoney(item.closing_balance, pulse.currency)}`), "本期暂无已过账的总账变动。");
    } catch (error) { setStatus("pulse-status", error.message, "error"); }
  }

  async function loadAnalytics() {
    try {
      const [analytics, metrics] = await Promise.all([api(`/pulse/analytics?date_from=${pulse.dateFrom}&date_to=${pulse.dateTo}`), api("/pulse/metrics")]);
      renderList($("pulse-analytics-output"), analytics.sales.revenue_by_sku, (item) => card(item.name, `${item.orders} 笔订单`, escapeMoney(item.revenue, pulse.currency)), "尚无可分析的收入记录。");
      renderList($("pulse-segment-output"), analytics.customers, (item) => card(item.name, `${item.segment} · ${item.segment_rule}`, `${item.orders} 笔订单 · ${escapeMoney(item.lifetime_revenue, pulse.currency)}`), "尚无客户行为数据。");
      renderList($("pulse-metric-dictionary"), metrics, (item) => card(item.name, `${item.time_grain} · ${item.data_source}`, `${item.formula}\n限制：${item.limitations}`), "指标字典尚未初始化。");
    } catch (error) { setStatus("pulse-status", error.message, "error"); }
  }

  leapDialog.querySelectorAll("[data-domain-tab]").forEach((button) => button.addEventListener("click", () => activate(leapDialog, button.dataset.domainTab)));
  pulseDialog.querySelectorAll("[data-domain-tab]").forEach((button) => button.addEventListener("click", async () => {
    activate(pulseDialog, button.dataset.domainTab);
    if (button.dataset.domainTab === "finance") await loadFinance();
    if (button.dataset.domainTab === "analytics") await loadAnalytics();
  }));
  $("leap-domain-close").addEventListener("click", () => leapDialog.close());
  $("pulse-domain-close").addEventListener("click", () => pulseDialog.close());
  leapDialog.addEventListener("cancel", (event) => { event.preventDefault(); leapDialog.close(); });
  pulseDialog.addEventListener("cancel", (event) => { event.preventDefault(); pulseDialog.close(); });

  return {
    async openLeap() {
      if (!leapDialog.open) leapDialog.showModal();
      activate(leapDialog, "library");
      try { await loadLeapBase(); } catch (error) { setStatus("leap-status", error.message, "error"); }
    },
    async openPulse() {
      if (!pulseDialog.open) pulseDialog.showModal();
      activate(pulseDialog, "overview");
      try { await loadPulse(); } catch (error) { setStatus("pulse-status", error.message, "error"); }
    },
  };
}
