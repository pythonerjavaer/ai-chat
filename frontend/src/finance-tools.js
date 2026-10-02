const money = (value, currency = "AUD") => new Intl.NumberFormat("en-AU", {
  style: "currency", currency, maximumFractionDigits: 0,
}).format(Number(value || 0));
const pct = (value) => `${(Number(value || 0) * 100).toFixed(2)}%`;
const precisePct = (value) => `${(Number(value) * 100).toFixed(3)}%`;
const moneyMillions = (value, currency = "AUD") => `${currency === "AUD" ? "A$" : `${currency} `}${new Intl.NumberFormat("en-AU", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
}).format(Number(value))}m`;
const strategyName = (key) => key === "balanced" ? "固定平衡型" : "生命周期型";
const lifecycleAssumptions = "年度独立正态收益，最低按 -95% 截断；年末缴费；未纳入税费、提取及养老金制度规则；生命周期策略在积累期后段逐步降风险";
const acquisitionAssumptions = "仅为简化的交易前后测算，并非审计合并报表；现金支付减少合并流动资产，一年内到期的新债增加流动负债；仅在用户输入时计入协同现金流；税率和资本成本采用用户填写的假设";

function fieldNumber(form, name) {
  const raw = new FormData(form).get(name);
  return raw === "" || raw == null ? null : Number(raw);
}

function metric(label, value, note = "") {
  const node = document.createElement("article"); node.className = "ember-result-metric";
  const title = document.createElement("small"); title.textContent = label;
  const result = document.createElement("strong"); result.textContent = String(value);
  const caption = document.createElement("span"); caption.textContent = note;
  node.append(title, result, caption); return node;
}

function safeFormPayload(form, optional = []) {
  const payload = {};
  new FormData(form).forEach((raw, key) => {
    payload[key] = raw === "" && optional.includes(key) ? null : Number(raw);
  });
  return payload;
}

function configureAgeSelectors(form) {
  const fields = ["current_age", "retirement_age", "shock_age"];
  const initial = {};
  const selects = Object.fromEntries(fields.map((name) => {
    const input = form.elements.namedItem(name);
    initial[name] = input.value;
    const select = document.createElement("select");
    select.name = name;
    select.id = input.id;
    select.className = input.className;
    select.required = input.required;
    select.setAttribute("aria-label", name === "current_age" ? "当前年龄（整岁）"
      : name === "retirement_age" ? "退休年龄（整岁）" : "市场冲击年龄（可选）");
    input.replaceWith(select);
    return [name, select];
  }));
  const previous = { current_age: initial.current_age || "35", retirement_age: initial.retirement_age || "67", shock_age: initial.shock_age || "" };

  function populate(select, options, selected) {
    select.replaceChildren();
    options.forEach(({ value, label, disabled = false }) => {
      const option = document.createElement("option");
      option.value = value; option.textContent = label; option.disabled = disabled;
      option.selected = value === selected; select.append(option);
    });
    if (!select.value || select.selectedOptions[0]?.disabled) {
      const first = [...select.options].find((option) => !option.disabled);
      if (first) select.value = first.value;
    }
  }
  function refresh() {
    const currentAge = Number(selects.current_age.value || 35);
    const retirementAge = Number(selects.retirement_age.value || 67);
    populate(selects.current_age, Array.from({ length: 82 }, (_, index) => {
      const value = String(index + 18);
      return { value, label: `${value} 岁`, disabled: Number(value) >= retirementAge };
    }), selects.current_age.value || previous.current_age);
    populate(selects.retirement_age, Array.from({ length: 82 }, (_, index) => {
      const value = String(index + 19);
      return { value, label: `${value} 岁`, disabled: Number(value) <= currentAge };
    }), selects.retirement_age.value || previous.retirement_age);
    const activeCurrent = Number(selects.current_age.value || currentAge);
    const activeRetirement = Number(selects.retirement_age.value || retirementAge);
    populate(selects.shock_age, [
      { value: "", label: "无冲击（可选）" },
      ...Array.from({ length: Math.max(0, activeRetirement - activeCurrent) }, (_, index) => {
        const value = String(activeCurrent + index);
        return { value, label: `${value} 岁` };
      }),
    ], selects.shock_age.value || "");
  }
  Object.values(selects).forEach((select) => select.addEventListener("change", refresh));
  refresh();
}

function renderProjection(host, strategies) {
  host.replaceChildren();
  const names = ["balanced", "lifecycle"].filter((key) => strategies[key]?.projection?.length);
  if (!names.length) return;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 620 220"); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "退休余额中位数模拟路径");
  const rows = strategies[names[0]].projection, all = names.flatMap((name) => strategies[name].projection.map((row) => row.median_balance));
  const max = Math.max(1, ...all), colors = { balanced: "#ffb979", lifecycle: "#69e2bf" };
  const points = {};
  names.forEach((name) => {
    const values = strategies[name].projection;
    const list = values.map((row, index) => `${42 + index * 560 / Math.max(1, values.length - 1)},${184 - row.median_balance / max * 160}`).join(" ");
    const path = document.createElementNS(svg.namespaceURI, "polyline"); path.setAttribute("points", list); path.setAttribute("fill", "none"); path.setAttribute("stroke", colors[name]); path.setAttribute("stroke-width", "3");
    svg.append(path); points[name] = values.at(-1)?.median_balance;
  });
  const label = document.createElement("p"); label.className = "ember-chart-legend";
  label.textContent = names.map((name) => `${strategyName(name)}: ${money(points[name])}`).join("　·　");
  host.append(svg, label);
}

export function initFinanceTools(api) {
  const panel = document.querySelector("#ember-finance-lab");
  const lifecycleForm = document.querySelector("#ember-lifecycle-form");
  const acquisitionForm = document.querySelector("#ember-acquisition-form");
  const eyebrow = document.querySelector("#domain-model-eyebrow");
  const title = document.querySelector("#domain-model-title");
  const description = document.querySelector("#domain-model-description");
  configureAgeSelectors(lifecycleForm);
  const targetEbitField = acquisitionForm.elements.namedItem("target_ebit");
  acquisitionForm.querySelectorAll("[data-case-ebit]").forEach((button) => {
    button.addEventListener("click", () => {
      targetEbitField.value = button.dataset.caseEbit;
      targetEbitField.dataset.source = "Australian Vintage FY2023 annual report (ASX)";
      const status = document.querySelector("#ember-acquisition-status");
      status.textContent = `已填入 AVG FY2023 ${button.dataset.caseBasis}：AUD ${button.dataset.caseEbit}m（来源：ASX 官方财报）。`;
      status.dataset.state = "info";
    });
  });
  lifecycleForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = document.querySelector("#ember-lifecycle-status"); const host = document.querySelector("#ember-lifecycle-results");
    status.textContent = "正在运行固定随机种子的模拟…"; status.dataset.state = "busy";
    const payload = safeFormPayload(lifecycleForm, ["shock_age"]);
    try {
      const result = await api("/finance/models/lifecycle", { method: "POST", body: JSON.stringify(payload) });
      host.replaceChildren();
      ["balanced", "lifecycle"].forEach((key) => {
        const row = result.strategies[key]; const card = document.createElement("section"); card.className = "ember-strategy-card";
        const title = document.createElement("h4"); title.textContent = strategyName(key);
        const grid = document.createElement("div"); grid.className = "ember-result-grid";
        grid.append(metric("退休余额中位数", money(row.median_terminal_balance), `P10 ${money(row.p10_terminal_balance)} · P90 ${money(row.p90_terminal_balance)}`),
          metric("年化波动率", pct(row.annual_return_volatility)), metric("平均最大回撤", pct(row.average_max_drawdown)),
          metric("目标达成率", pct(row.target_success_rate)));
        card.append(title, grid); host.append(card);
      });
      const chart = document.createElement("div"); chart.className = "ember-projection-chart"; renderProjection(chart, result.strategies); host.append(chart);
      const forecastTitle = document.createElement("h4"); forecastTitle.textContent = "确定性情景终值（与随机模拟分开）"; host.append(forecastTitle);
      const forecastTable = document.createElement("table"); forecastTable.className = "ember-sensitivity-table";
      const forecastHead = document.createElement("tr"); ["配置策略", "悲观", "基准", "乐观"].forEach((text) => { const cell = document.createElement("th"); cell.textContent = text; forecastHead.append(cell); }); forecastTable.append(forecastHead);
      ["balanced", "lifecycle"].forEach((key) => { const strategy = result.strategies[key]; const tr = document.createElement("tr");
        [strategyName(key), ...["bear", "base", "bull"].map((scenario) => money(strategy.forecast_scenarios[scenario].terminal_balance))].forEach((text) => { const cell = document.createElement("td"); cell.textContent = text; tr.append(cell); }); forecastTable.append(tr); });
      host.append(forecastTable);
      const note = document.createElement("p"); note.className = "ember-assumption-note"; note.textContent = `蒙特卡洛模拟 ${result.simulations.toLocaleString()} 次 · 随机种子 ${result.seed} · ${lifecycleAssumptions}`; host.append(note);
      status.textContent = "已完成。切换年龄、缴费、收益/波动假设或冲击情景后可重新运行。"; status.dataset.state = "ok";
    } catch (error) { status.textContent = error.message; status.dataset.state = "error"; }
  });
  acquisitionForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const status = document.querySelector("#ember-acquisition-status"); const host = document.querySelector("#ember-acquisition-results");
    status.textContent = "正在计算融资与利率敏感性…"; status.dataset.state = "busy";
    const payload = safeFormPayload(acquisitionForm, [
      "target_ebit", "operating_cash_flow", "maintenance_capex", "acquirer_ebit",
      "existing_interest_expense", "acquirer_current_assets", "acquirer_current_liabilities",
      "target_current_assets", "target_current_liabilities", "new_debt_current_portion",
    ]); payload.currency = "AUD";
    try {
      const result = await api("/finance/models/acquisition", { method: "POST", body: JSON.stringify(payload) });
      host.replaceChildren(); const grid = document.createElement("div"); grid.className = "ember-result-grid";
      grid.append(metric("新增债务", moneyMillions(result.new_debt, result.currency), `${payload.debt_share * 100}% 债务融资`),
        metric("现金对价", moneyMillions(result.cash_consideration, result.currency)),
        metric("新增年利息", moneyMillions(result.annual_incremental_interest, result.currency)),
        metric("简化 EBIT / 新增利息", result.interest_coverage == null ? "需要 EBIT" : `${result.interest_coverage.toFixed(2)}×`, "不等同于合并口径利息保障倍数"),
        metric("交易前 WACC", result.wacc_before == null ? "—" : precisePct(result.wacc_before)),
        metric("交易后 WACC", result.wacc_after == null ? "—" : precisePct(result.wacc_after)));
      host.append(grid);
      if (result.pro_forma) {
        const proTitle = document.createElement("h4"); proTitle.textContent = "交易前后财务结构（仅按已录入数据测算）";
        const proGrid = document.createElement("div"); proGrid.className = "ember-result-grid";
        const ratio = (value) => value == null ? "需补充输入" : `${value.toFixed(2)}×`;
        const percent = (value) => value == null ? "需补充输入" : pct(value);
        proGrid.append(
          metric("利息保障倍数 · 交易前", ratio(result.pro_forma.interest_coverage_before)),
          metric("利息保障倍数 · 简化交易后", ratio(result.pro_forma.interest_coverage_after)),
          metric("流动比率 · 交易前", ratio(result.pro_forma.current_ratio_before)),
          metric("流动比率 · 简化交易后", ratio(result.pro_forma.current_ratio_after)),
          metric("债务 / 资本 · 交易前", percent(result.pro_forma.debt_to_capital_before)),
          metric("债务 / 资本 · 交易后", percent(result.pro_forma.debt_to_capital_after)),
        );
        host.append(proTitle, proGrid);
      }
      const heading = document.createElement("h4"); heading.textContent = "债务利率敏感性"; host.append(heading);
      const table = document.createElement("table"); table.className = "ember-sensitivity-table";
      const header = document.createElement("tr"); ["利率冲击", "债务利率", "年利息", "EBIT / 新增利息", "利息后现金流"].forEach((text) => { const cell = document.createElement("th"); cell.textContent = text; header.append(cell); }); table.append(header);
      result.rate_sensitivity.forEach((row) => { const tr = document.createElement("tr");
        [pct(row.rate_shock), pct(row.debt_rate), moneyMillions(row.interest, result.currency), row.interest_coverage == null ? "—" : `${row.interest_coverage.toFixed(2)}×`, row.cash_buffer_after_interest == null ? "缺少现金流输入" : moneyMillions(row.cash_buffer_after_interest, result.currency)].forEach((text) => { const cell = document.createElement("td"); cell.textContent = text; tr.append(cell); }); table.append(tr); });
      host.append(table);
      const mixTitle = document.createElement("h4"); mixTitle.textContent = "债务 / 现金融资结构敏感性"; host.append(mixTitle);
      const mixTable = document.createElement("table"); mixTable.className = "ember-sensitivity-table";
      const mixHeader = document.createElement("tr"); ["债务比例", "现金比例", "新增债务", "现金对价", "年利息", "WACC"].forEach((text) => { const cell = document.createElement("th"); cell.textContent = text; mixHeader.append(cell); }); mixTable.append(mixHeader);
      result.financing_mix_sensitivity.forEach((row) => { const tr = document.createElement("tr");
        [pct(row.debt_share), pct(row.cash_share), moneyMillions(row.new_debt, result.currency), moneyMillions(row.cash_consideration, result.currency), moneyMillions(row.annual_interest, result.currency), row.wacc == null ? "需补资本结构" : precisePct(row.wacc)].forEach((text) => { const cell = document.createElement("td"); cell.textContent = text; tr.append(cell); }); mixTable.append(tr); });
      host.append(mixTable);
      const stressTitle = document.createElement("h4"); stressTitle.textContent = "简化 EBIT / 新增利息 × 利率压力情景"; host.append(stressTitle);
      const stressNote = document.createElement("p"); stressNote.className = "ember-assumption-note";
      stressNote.textContent = result.missing_inputs.includes("target_ebit") ? "输入标的 EBIT 后显示覆盖倍数矩阵。" : result.operating_stress_matrix.map((row) => `EBIT ${(row.ebit_change * 100).toFixed(0)}% / 利率冲击 ${(row.rate_shock * 100).toFixed(0)}%：${row.interest_coverage ?? "—"}×`).join("　·　");
      host.append(stressNote);
      const graphStatus = document.createElement("p"); graphStatus.className = "ember-assumption-note";
      graphStatus.textContent = result.graphdb?.status === "stored"
        ? `GraphDB 已记录 ${result.graphdb.triples_written} 条情景关系；本次运行历史已保存。`
        : `GraphDB 当前${result.graphdb?.status === "not_configured" ? "未配置" : "不可用"}；本次运行历史已保存到当前业务数据库。`;
      host.append(graphStatus);
      const inputLabels = { target_ebit: "标的 EBIT", operating_cash_flow: "经营现金流", maintenance_capex: "维护资本开支" };
      const missingInputs = result.missing_inputs.map((key) => inputLabels[key] || key);
      const note = document.createElement("p"); note.className = "ember-assumption-note";
      note.textContent = `${missingInputs.length ? `待补充输入：${missingInputs.join("、")}。` : ""}简化覆盖指标只比较标的 EBIT 与本次新增债务利息；交易前后利息保障倍数按补充财务输入单独计算，不等同于审计合并报表。${acquisitionAssumptions}`; host.append(note);
      status.textContent = "已完成。可调整交易、债务比例、资金成本及经营输入重新测算。"; status.dataset.state = "ok";
    } catch (error) { status.textContent = error.message; status.dataset.state = "error"; }
  });
  return { show(workspace) {
    const isAurora = workspace === "general";
    const isEmber = workspace === "finance";
    panel.classList.toggle("hidden", !isAurora && !isEmber);
    lifecycleForm.classList.toggle("hidden", !isAurora);
    acquisitionForm.classList.toggle("hidden", !isEmber);
    panel.setAttribute("aria-label", isAurora ? "养老金预测模型" : "企业并购财务模型");
    eyebrow.textContent = isAurora ? "AURORA / FUTURE FORECAST" : "EMBER / CORPORATE FINANCE";
    title.textContent = isAurora ? "生命周期预测实验台" : "企业并购决策实验台";
    description.textContent = isAurora
      ? "基于可编辑的人口、缴费与市场假设运行长期情景预测；未来雷达负责外部机会探索。"
      : "对交易结构、融资成本、偿债能力与经营压力进行可复核的情景分析。结果不是投资建议。";
  } };
}
