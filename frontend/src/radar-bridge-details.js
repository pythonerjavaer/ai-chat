export const BRIDGE_FILTERS = Object.freeze({
  overview: "同步与来源", source_screened: "ChatGPT 已筛选", accepted: "已核验信号", pending: "尚未入池", rejected: "未通过核验", needs_review: "需要查看", all: "全部信号",
});

export function bridgeDetailQuery(filter = "all", page = 1) {
  const status = Object.hasOwn(BRIDGE_FILTERS, filter) && filter !== "overview" ? filter : "all";
  const query = new URLSearchParams({ review_status: status, limit: "50", offset: String((Math.max(1, Number(page) || 1) - 1) * 50) });
  if (status !== "needs_review") query.set("source_scope", "chatgpt");
  return `/future-radar/review-candidates?${query}`;
}

function publicUrl(value) {
  try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : ""; } catch { return ""; }
}

export function bridgeCandidateCanAdd(item) {
  // No override based on label text: the API knows closed and expired records.
  return item.can_add_to_pool === true;
}

export function initBridgeDetails({ root, api, make, formatTime, getSyncStatus, errorCopy = error => error.message, onManualRead = () => {}, onAdded = async () => {} }) {
  const state = { filter: "overview", page: 1, payload: null, loading: false, error: "", feedback: "", requestId: 0, session: 0 };
  const action = (label, callback, disabled = false) => {
    const button = make("button", "radar-source-filter", label); button.type = "button"; button.disabled = disabled; button.addEventListener("click", callback); return button;
  };
  function renderOverview() {
    const status = getSyncStatus();
    root.append(make("p", "recruitment-disclaimer", "这里显示同步桥已回传的状态。查看记录不会读取 ChatGPT 对话，也不会触发 AI 或外部扫描。"));
    if (!status) { root.append(make("div", "empty-list", "同步状态尚未读取。返回全部机会后刷新机会，或等待当前读取完成。")); return; }
    const sources = status.sources || [];
    root.append(make("p", "radar-entity-meta", `最后同步 ${formatTime(status.last_synced_at || status.last_sync_at || status.last_completed_at || status.updated_at, "尚无记录")} · ${sources.length} 个已回传来源`));
    if (!sources.length) root.append(make("div", "empty-list", "当前响应没有来源明细；不能把未返回的来源当成同步成功。"));
    sources.forEach(source => {
      const card = make("article", "radar-entity-card");
      card.append(make("h4", "", source.title || source.name || "ChatGPT 监控源"), make("p", "radar-entity-meta", `最后回传 ${formatTime(source.last_seen_at || source.last_synced_at, "尚无记录")}`));
      const statuses = { synced: "已同步", healthy: "已回传", active: "已回传", success: "已回传", pending: "等待回传", error: "回传失败", failed: "回传失败", disabled: "已停用", paused: "已暂停" };
      const sourceState = String(source.status || source.state || "");
      if (sourceState) card.append(make("p", "radar-entity-meta", statuses[sourceState] || "状态待确认"));
      const counts = [["source_screened", "已筛选"], ["accepted", "已核验"], ["pending", "待入池"], ["rejected", "未通过"]].map(([key, label]) => [source[`inventory_${key}`] ?? source[key], label]).filter(([value]) => value != null).map(([value, label]) => `${label} ${value}`);
      if (counts.length) card.append(make("p", "radar-entity-meta", counts.join(" · ")));
      root.append(card);
    });
  }
  function render() {
    if (!root) return;
    root.replaceChildren(); root.setAttribute("aria-busy", String(state.loading));
    const heading = make("header", "radar-candidate-heading");
    const identity = make("div"); identity.append(make("span", "overline", "CONTROLLED CHAT BRIDGE"), make("h3", "", "同步信号明细"), make("p", "", "按同步桥保留的原始信号查看；同一岗位可能有多条来源记录，数量与合并后的机会池不同。"));
    heading.append(identity, state.filter === "overview" ? action("查看全部信号", () => open("all")) : action("刷新明细", () => load(), state.loading)); root.append(heading);
    const toolbar = make("div", "radar-source-controls");
    Object.entries(BRIDGE_FILTERS).forEach(([key, label]) => {
      const button = action(label, () => open(key)); button.setAttribute("aria-pressed", String(state.filter === key)); toolbar.append(button);
    }); root.append(toolbar);
    if (state.feedback) root.append(make("p", "radar-action-status", state.feedback));
    if (state.filter === "overview") { renderOverview(); return; }
    if (state.loading) { root.append(make("div", "empty-list", `正在读取“${BRIDGE_FILTERS[state.filter]}”记录…`)); return; }
    if (state.error) { const error = make("p", "radar-load-error", `信号明细读取失败：${state.error}。请点击“刷新明细”重试。`); error.setAttribute("role", "alert"); root.append(error); return; }
    const payload = state.payload;
    if (!payload) return;
    if (payload.notice) root.append(make("p", "recruitment-disclaimer", payload.notice));
    if (!payload.items?.length) root.append(make("div", "empty-list", `当前没有“${BRIDGE_FILTERS[state.filter]}”记录。`));
    (payload.items || []).forEach(item => {
      const card = make("article", "job-card radar-review-candidate");
      card.append(make("h4", "", `${item.company || "招聘机构待确认"}｜${item.title || "岗位待确认"}`), make("p", "job-meta", [item.city, item.industry, item.closing_date ? `截止 ${item.closing_date}` : "截止时间待确认"].filter(Boolean).join(" · ")), make("p", "", `${item.review_status_label || "状态待确认"}：${item.review_reason || "未提供详细原因"}`));
      if (item.source_name) card.append(make("p", "radar-entity-meta", `来源：${item.source_name}`));
      if (item.last_seen_at) card.append(make("p", "radar-entity-meta", `最近同步 ${formatTime(item.last_seen_at)}`));
      (item.evidence || []).forEach(evidence => { if (typeof evidence === "string") card.append(make("p", "radar-entity-meta", evidence)); });
      const url = publicUrl(item.official_url);
      if (url) { const link = make("a", "radar-official-link", "打开公开招聘链接 ↗"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; card.append(link); }
      if (bridgeCandidateCanAdd(item)) {
        const add = action("加入机会池", async () => {
          const session = state.session; add.disabled = true; add.textContent = "正在加入…";
          try {
            const result = await api(`/future-radar/review-candidates/${encodeURIComponent(item.id)}/add-to-pool`, { method: "POST" });
            if (session !== state.session) return;
            state.feedback = result.notice || "已加入机会池；仍保留原核验状态说明。";
            await onAdded(); await load();
          } catch (error) { if (session === state.session) { state.feedback = `加入失败：${errorCopy(error)}`; render(); } }
        }); card.append(add);
      }
      root.append(card);
    });
    const total = Number(payload.total) || 0; const pages = Math.max(1, Math.ceil(total / 50));
    const pagination = make("div", "radar-title-pagination");
    pagination.append(action("上一页", () => { state.page -= 1; return load(); }, state.page <= 1), make("span", "radar-entity-meta", `第 ${state.page} / ${pages} 页 · ${total} 条信号`), action("下一页", () => { state.page += 1; return load(); }, state.page >= pages));
    root.append(pagination);
  }
  async function load() {
    const request = ++state.requestId;
    if (state.filter === "overview") { state.loading = false; render(); return; }
    state.loading = true; state.error = ""; render(); onManualRead();
    try { const payload = await api(bridgeDetailQuery(state.filter, state.page)); if (request === state.requestId) state.payload = payload; }
    catch (error) { if (request === state.requestId) state.error = errorCopy(error); }
    finally { if (request === state.requestId) { state.loading = false; render(); } }
  }
  function open(filter = "overview") { state.filter = Object.hasOwn(BRIDGE_FILTERS, filter) ? filter : "overview"; state.page = 1; state.feedback = ""; return load(); }
  return { open, refreshOverview() { if (state.filter === "overview") render(); }, reset() { state.session += 1; state.requestId += 1; Object.assign(state, { filter: "overview", page: 1, payload: null, loading: false, error: "", feedback: "" }); root?.replaceChildren(); } };
}
