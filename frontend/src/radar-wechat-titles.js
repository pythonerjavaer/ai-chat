export function wechatTitleQuery({ page = 1, sourceName = "", relevance = "", fromDate = "", toDate = "" } = {}) {
  const query = new URLSearchParams({ page: String(Math.max(1, Number(page) || 1)), page_size: "30" });
  if (sourceName) query.set("source_name", sourceName);
  if (relevance) query.set("relevance_status", relevance);
  if (fromDate) query.set("from_date", fromDate);
  if (toDate) query.set("to_date", toDate);
  return `/sources/wechat/articles?${query}`;
}

export function wechatArticleUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && url.hostname === "mp.weixin.qq.com" && !url.username && !url.password ? url.href : "";
  } catch { return ""; }
}

export function parseWechatImportUrls(value) {
  const urls = String(value || "").split(/\s+/).map(url => url.trim()).filter(Boolean);
  if (!urls.length) throw new Error("请先粘贴至少一条公开文章链接。");
  if (urls.length > 50) throw new Error("每批最多导入 50 条链接，请分批提交。");
  if (urls.some(url => !wechatArticleUrl(url))) throw new Error("只接受 https://mp.weixin.qq.com/ 开头的公开文章链接。请检查后重试。");
  return [...new Set(urls)];
}

export function wechatImportSummary(result) {
  if (!result) return "";
  return `本批 ${result.total ?? "—"} 条：成功 ${result.success ?? "—"}，新增 ${result.new ?? "—"}，重复 ${result.duplicate ?? "—"}，失败 ${result.failed ?? "—"}。失败链接不会影响其他记录。`;
}

export function wechatFetchStatusCopy(value) {
  return { success: "已读取标题", timeout: "读取超时", http_403: "访问受限（403）", http_404: "文章不存在（404）", http_error: "页面返回错误", network_error: "网络连接失败", parse_failed: "未能解析标题", access_restricted: "公开访问受限", invalid_url: "链接不符合要求", unsafe_url: "链接或跳转地址不允许", blocked: "公开页面限制访问", storage_or_processing_error: "保存或处理失败" }[value] || "状态待确认";
}

export function initWechatTitleRadar({ root, api, make, formatTime, errorCopy = error => error.message }) {
  const state = { payload: null, sources: null, sourcesError: "", page: 1, sourceName: "", relevance: "", fromDate: "", toDate: "", urls: "", expectedSource: "", forceRefresh: false, loading: false, importing: false, error: "", importError: "", result: null, requestId: 0, session: 0 };
  const action = (label, callback, disabled = false) => {
    const button = make("button", "radar-source-filter", label); button.type = "button"; button.disabled = disabled; button.addEventListener("click", callback); return button;
  };
  const selectControl = (label, value, options, callback) => {
    const field = make("label"); field.append(make("span", "", label));
    const select = make("select"); select.setAttribute("aria-label", label);
    options.forEach(([key, text]) => { const option = make("option", "", text); option.value = key; select.append(option); });
    select.value = value; select.addEventListener("change", () => callback(select.value)); field.append(select); return field;
  };
  function renderImportForm() {
    const form = make("form", "radar-title-import");
    const label = make("label"); label.append(make("span", "", "公开文章链接（每行一条，最多 50 条）"));
    const input = make("textarea"); input.rows = 4; input.value = state.urls; input.placeholder = "https://mp.weixin.qq.com/s/…"; input.required = true;
    input.setAttribute("aria-label", "公开文章链接"); input.addEventListener("input", () => { state.urls = input.value; }); label.append(input); form.append(label);
    const options = (state.sources?.items || []).map(source => [source.source_name, source.source_name]);
    form.append(selectControl("归属公众号（可选，混合来源请留空）", state.expectedSource, [["", "优先从页面识别"], ...options], value => { state.expectedSource = value; }));
    const forceLabel = make("label", "radar-title-toggle"); const force = make("input"); force.type = "checkbox"; force.checked = state.forceRefresh;
    force.addEventListener("change", () => { state.forceRefresh = force.checked; }); forceLabel.append(force, make("span", "", "重新读取已保存链接（默认复用缓存）")); form.append(forceLabel);
    const button = make("button", "radar-source-filter", state.importing ? "正在读取标题…" : "导入并识别标题"); button.type = "submit"; button.disabled = state.importing; form.append(button);
    const watchlistCount = (state.sources?.items || []).filter(source => source.enabled !== false && wechatArticleUrl(source.seed_url)).length;
    const importWatchlist = action(
      state.importing ? "正在导入观察名单…" : `一键导入 ${watchlistCount || "—"} 个历史入口`,
      importWatchlistSeeds,
      state.importing || watchlistCount === 0,
    );
    importWatchlist.className += " radar-title-watchlist-import";
    form.append(importWatchlist, make("p", "radar-entity-meta", "一次导入观察名单中已配置的历史文章，并保留各自公众号归属；它不会自动发现该公众号之后发布的新文章。"));
    form.addEventListener("submit", event => { event.preventDefault(); return importUrls(); }); root.append(form);
    if (state.importing) root.append(make("p", "radar-loading", "正在逐条读取文章元信息；较大批次可能需要几分钟。页面受限或单条失败不会终止整批。"));
    if (state.importError) { const error = make("p", "radar-load-error", state.importError); error.setAttribute("role", "alert"); root.append(error); }
    if (state.result) {
      const result = make("section", "radar-title-import-result"); const summary = make("p", "radar-action-status", wechatImportSummary(state.result)); summary.setAttribute("role", "status"); result.append(summary);
      const details = make("details", "radar-title-coverage"); details.open = Number(state.result.failed) > 0; details.append(make("summary", "", "查看本批每条链接的处理结果"));
      (state.result.items || []).forEach(item => {
        const row = make("p", "radar-entity-meta"); const url = wechatArticleUrl(item.url);
        row.append(make("span", "", `${wechatFetchStatusCopy(item.fetch_status)} · ${item.is_new === true ? "新增" : item.fetch_status === "success" ? "已有记录" : "未成功读取"} · `));
        if (url) { const link = make("a", "radar-title-link", item.title || url); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link); }
        else row.append(make("span", "", item.title || "链接无效"));
        details.append(row);
      }); result.append(details); root.append(result);
    }
  }
  function renderSources() {
    const details = make("details", "radar-title-coverage"); details.append(make("summary", "", "公众号观察名单与文章入口"));
    if (state.sourcesError) details.append(make("p", "radar-load-error", `观察名单读取失败：${state.sourcesError}`));
    (state.sources?.items || []).forEach(source => {
      const row = make("div", "radar-title-source");
      row.append(make("strong", "", source.source_name), make("span", "radar-entity-meta", `最近检查 ${formatTime(source.last_checked_at, "尚未检查")} · ${source.enabled === false ? "已停用" : "手动链接导入"} · 已保存 ${source.total_articles ?? "—"} 篇 · 近7天首次发现 ${source.new_articles ?? "—"} 篇`));
      const url = wechatArticleUrl(source.seed_url);
      if (url) { const link = make("a", "radar-official-link", "历史文章入口 ↗"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; row.append(link); }
      details.append(row);
    });
    details.append(make("p", "radar-entity-meta", "这些入口是历史文章，不是公众号文章列表；不能据此获取该账号全部新文章。"));
    root.append(details);
  }
  function render() {
    if (!root) return;
    root.replaceChildren(); root.setAttribute("aria-busy", String(state.loading || state.importing));
    const heading = make("header", "radar-candidate-heading"); const identity = make("div");
    identity.append(make("span", "overline", "WECHAT TITLE RADAR"), make("h3", "", "微信公众号情报源"), make("p", "", "粘贴公开文章链接，读取标题、公众号与可靠的发布时间，再用本地规则识别招聘线索。文章不会直接成为已核验岗位。"));
    heading.append(identity, action("刷新记录", load, state.loading || state.importing)); root.append(heading);
    root.append(make("p", "radar-title-boundary", "手动导入 · 仅保存元信息 · 不使用付费 API · 不消耗模型 Token"));
    root.append(make("p", "radar-action-status", "自动搜索发现尚无可靠的零成本渠道，当前使用手动链接导入。历史文章入口不能列出账号的全部新文章；同步 ChatGPT 时也不会自动逐篇读取文章链接。"));
    renderImportForm(); renderSources();
    const toolbar = make("div", "radar-title-filters");
    toolbar.append(selectControl("公众号", state.sourceName, [["", "全部公众号"], ...(state.sources?.items || []).map(source => [source.source_name, source.source_name])], value => { state.sourceName = value; state.page = 1; return load(); }));
    toolbar.append(selectControl("标题相关性", state.relevance, [["", "全部标题"], ["relevant", "招聘相关"], ["possible", "可能相关"], ["irrelevant", "不相关"]], value => { state.relevance = value; state.page = 1; return load(); }));
    [["fromDate", "发布日期起"], ["toDate", "发布日期止"]].forEach(([key, label]) => {
      const field = make("label"); field.append(make("span", "", label)); const input = make("input"); input.type = "date"; input.value = state[key]; input.setAttribute("aria-label", label);
      input.addEventListener("change", () => { state[key] = input.value; state.page = 1; return load(); }); field.append(input); toolbar.append(field);
    }); root.append(toolbar);
    if (state.loading) root.append(make("p", "radar-loading", "正在读取已保存的文章标题…"));
    if (state.error) root.append(make("p", "radar-load-error", `标题记录读取失败：${state.error}。请点击“刷新记录”重试。`));
    if (!state.payload || state.loading || state.error) return;
    const payload = state.payload; const list = make("div", "radar-entity-list");
    if (!payload.items?.length) list.append(make("div", "empty-list", "当前筛选下尚无已保存标题。导入链接后将在这里显示；这不表示公众号没有发布新文章。"));
    (payload.items || []).forEach(item => {
      const card = make("article", "radar-entity-card radar-title-card"); const top = make("div", "radar-entity-top");
      const relevant = ["relevant", "possible"].includes(item.relevance_status);
      top.append(make("span", "radar-source-type", item.source_name || "公众号名称未知"), make("span", "radar-status-badge warning", relevant ? "招聘线索 · 待官网核验" : wechatFetchStatusCopy(item.fetch_status)));
      const title = make("h4"); const url = wechatArticleUrl(item.url);
      if (url) { const link = make("a", "radar-title-link", item.title || "标题未能读取"); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; title.append(link); } else title.append(make("span", "", item.title || "标题未能读取"));
      card.append(top, title, make("p", "radar-entity-meta", `发布时间 ${formatTime(item.published_at, "未知")} · 发现 ${formatTime(item.discovered_at, "时间未知")}`));
      const relevance = { relevant: "招聘相关", possible: "可能相关", irrelevant: "不相关" }[item.relevance_status] || "未分类";
      card.append(make("p", "radar-title-score", `${relevance} · 相关性 ${item.relevance_score ?? "—"} / 100 · ${wechatFetchStatusCopy(item.fetch_status)}`));
      if (item.matched_keywords?.length) card.append(make("p", "radar-entity-meta", `本地规则命中：${item.matched_keywords.join("、")}`));
      if (item.source_name_detection === "configured") card.append(make("p", "radar-entity-meta", "公众号名称来自导入时的配置，并非页面识别结果。"));
      if (relevant) card.append(make("p", "radar-entity-meta", "保留为招聘情报线索；具体岗位与有效报名条件需以企业官网 / ATS / 官方招聘公告确认。"));
      list.append(card);
    }); root.append(list);
    const total = Number(payload.total) || 0; const pages = Math.max(1, Math.ceil(total / (Number(payload.page_size) || 30)));
    const pagination = make("div", "radar-title-pagination");
    pagination.append(action("上一页", () => { state.page -= 1; return load(); }, state.page <= 1), make("span", "radar-entity-meta", `第 ${state.page} / ${pages} 页 · ${total} 条记录`), action("下一页", () => { state.page += 1; return load(); }, state.page >= pages)); root.append(pagination);
  }
  async function load() {
    const request = ++state.requestId; state.loading = true; state.error = ""; state.sourcesError = ""; render();
    const [sources, articles] = await Promise.allSettled([api("/sources/wechat"), api(wechatTitleQuery(state))]);
    if (request !== state.requestId) return;
    if (sources.status === "fulfilled") state.sources = sources.value; else state.sourcesError = errorCopy(sources.reason);
    if (articles.status === "fulfilled") state.payload = articles.value; else state.error = errorCopy(articles.reason);
    state.loading = false; render();
  }
  async function importUrls() {
    if (state.importing) return;
    let urls;
    try { urls = parseWechatImportUrls(state.urls); } catch (error) { state.importError = error.message; render(); return; }
    const session = state.session; state.importing = true; state.importError = ""; state.result = null; render();
    try {
      const result = await api("/sources/wechat/articles/import", { method: "POST", body: JSON.stringify({ urls, ...(state.expectedSource ? { expected_source_name: state.expectedSource } : {}), force_refresh: state.forceRefresh }), timeoutMs: 300_000 });
      if (session !== state.session) return;
      state.result = result; state.page = 1; await load();
    } catch (error) { if (session === state.session) state.importError = `导入请求未完成：${errorCopy(error)}。部分链接可能已经保存；可刷新记录或重新提交，同一链接不会重复入库。`; }
    finally { if (session === state.session) { state.importing = false; render(); } }
  }
  async function importWatchlistSeeds() {
    if (state.importing) return;
    const session = state.session; state.importing = true; state.importError = ""; state.result = null; render();
    try {
      const suffix = state.forceRefresh ? "?force_refresh=true" : "";
      const result = await api(`/sources/wechat/articles/import-watchlist${suffix}`, {
        method: "POST", timeoutMs: 300_000,
      });
      if (session !== state.session) return;
      state.result = result; state.page = 1; await load();
    } catch (error) {
      if (session === state.session) state.importError = `一键导入未完成：${errorCopy(error)}。已成功保存的入口不会重复入库，可稍后重试。`;
    } finally {
      if (session === state.session) { state.importing = false; render(); }
    }
  }
  return { open: load, reset() { state.session += 1; state.requestId += 1; Object.assign(state, { payload: null, sources: null, sourcesError: "", urls: "", expectedSource: "", forceRefresh: false, result: null, error: "", importError: "", importing: false, loading: false, page: 1, sourceName: "", relevance: "", fromDate: "", toDate: "" }); root?.replaceChildren(); } };
}
