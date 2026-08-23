const STORAGE_KEY = "bingyan_oblivion_archive_v1";
const VALID_TYPES = new Set(["memory", "unfinished", "lost_world"]);
const TYPE_META = {
  memory: { label: "记忆史诗", english: "MEMORY EPIC", copy: "那些真正发生过的事，不应该只剩一个模糊的轮廓。", statuses: ["封存", "已打捞"] },
  unfinished: { label: "未完章节", english: "UNFINISHED CHAPTERS", copy: "暂停，不等于结束。", statuses: ["暂停", "重新开始", "已结束"] },
  lost_world: { label: "失落世界", english: "LOST WORLDS", copy: "每一个没有抵达的未来，也曾经真实地存在过。", statuses: ["未选择", "仍可能抵达", "已放下"] },
};

const state = {
  entries: [],
  view: "overview",
  editingId: null,
  pendingImport: null,
  initialized: false,
  notify: () => {},
};

const $ = (id) => document.getElementById(id);
const el = (tag, className = "", text = "") => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
};

function readEntries() {
  try {
    const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
    state.entries = Array.isArray(parsed) ? parsed.map(normalizeEntry).filter(Boolean) : [];
  } catch (_) {
    state.entries = [];
  }
}

function persist() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(state.entries));
}

function cleanText(value, max) {
  return typeof value === "string" ? value.trim().slice(0, max) : "";
}

function normalizeEntry(raw) {
  if (!raw || typeof raw !== "object" || !VALID_TYPES.has(raw.type)) return null;
  const title = cleanText(raw.title, 80);
  const content = cleanText(raw.content, 3000);
  if (!title || !content) return null;
  const meta = TYPE_META[raw.type];
  const status = meta.statuses.includes(raw.status) ? raw.status : meta.statuses[0];
  const occurredAt = /^\d{4}-\d{2}-\d{2}$/.test(String(raw.occurred_at || "")) ? raw.occurred_at : null;
  const createdAt = Number.isFinite(Date.parse(raw.created_at)) ? raw.created_at : new Date().toISOString();
  const updatedAt = Number.isFinite(Date.parse(raw.updated_at)) ? raw.updated_at : createdAt;
  return {
    id: cleanText(raw.id, 120) || crypto.randomUUID(),
    type: raw.type,
    title,
    content,
    occurred_at: occurredAt,
    time_is_blurred: Boolean(raw.time_is_blurred),
    tags: Array.isArray(raw.tags) ? [...new Set(raw.tags.map((item) => cleanText(item, 40)).filter(Boolean))].slice(0, 8) : [],
    why_keep: cleanText(raw.why_keep, 300),
    status,
    created_at: createdAt,
    updated_at: updatedAt,
  };
}

function dateLabel(entry) {
  if (entry.time_is_blurred || !entry.occurred_at) return "时间已经模糊";
  return new Date(`${entry.occurred_at}T00:00:00`).toLocaleDateString("zh-CN", { year: "numeric", month: "long", day: "numeric" });
}

function statusIsSalvaged(entry) {
  return ["已打捞", "重新开始", "仍可能抵达"].includes(entry.status);
}

function updateStats() {
  const latest = [...state.entries].sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at))[0];
  const stats = [
    ["全部条目", state.entries.length],
    ["记忆史诗", state.entries.filter((item) => item.type === "memory").length],
    ["未完章节", state.entries.filter((item) => item.type === "unfinished").length],
    ["失落世界", state.entries.filter((item) => item.type === "lost_world").length],
    ["已打捞", state.entries.filter(statusIsSalvaged).length],
    ["最近封存", latest ? new Date(latest.created_at).toLocaleDateString("zh-CN") : "尚未封存"],
  ];
  $("oblivion-stats").replaceChildren(...stats.map(([label, value], index) => {
    const card = el("article");
    card.append(el("small", "", `0${index + 1}`), el("strong", "", String(value)), el("span", "", label));
    return card;
  }));
}

function updateStatusFilter() {
  const select = $("oblivion-status-filter");
  const selected = select.value;
  const statuses = [...new Set(state.entries.map((entry) => entry.status))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.replaceChildren(new Option("全部", "all"), ...statuses.map((status) => new Option(status, status)));
  if (statuses.includes(selected)) select.value = selected;
}

function entryInTimeRange(entry, range) {
  if (range === "all") return true;
  if (range === "blurred") return entry.time_is_blurred || !entry.occurred_at;
  if (!entry.occurred_at || entry.time_is_blurred) return false;
  const age = (Date.now() - Date.parse(`${entry.occurred_at}T00:00:00`)) / (365.25 * 24 * 60 * 60 * 1000);
  if (range === "recent") return age <= 1;
  if (range === "one_three") return age > 1 && age <= 3;
  return age > 3;
}

function filteredEntries() {
  const query = $("oblivion-search").value.trim().toLocaleLowerCase("zh-CN");
  const type = $("oblivion-type-filter").value;
  const status = $("oblivion-status-filter").value;
  const time = $("oblivion-time-filter").value;
  const direction = $("oblivion-sort").value === "asc" ? 1 : -1;
  return state.entries.filter((entry) => {
    if (type !== "all" && entry.type !== type) return false;
    if (status !== "all" && entry.status !== status) return false;
    if (!entryInTimeRange(entry, time)) return false;
    if (!query) return true;
    return `${entry.title} ${entry.content} ${entry.tags.join(" ")}`.toLocaleLowerCase("zh-CN").includes(query);
  }).sort((a, b) => {
    const aTime = a.occurred_at ? Date.parse(`${a.occurred_at}T00:00:00`) : Date.parse(a.created_at);
    const bTime = b.occurred_at ? Date.parse(`${b.occurred_at}T00:00:00`) : Date.parse(b.created_at);
    return (aTime - bTime) * direction;
  });
}

function actionButton(copy, className, handler) {
  const button = el("button", className, copy);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function updateEntryState(entry, status, message) {
  entry.status = status;
  entry.updated_at = new Date().toISOString();
  persist();
  renderAll();
  state.notify(message);
}

function renderEntryCard(entry) {
  const meta = TYPE_META[entry.type];
  const card = el("article", `oblivion-entry-card ${entry.type}`);
  const top = el("header");
  const identity = el("div");
  identity.append(el("span", "", meta.english), el("strong", "", meta.label));
  top.append(identity, el("b", "", entry.status));
  const tags = el("div", "oblivion-entry-tags");
  entry.tags.forEach((tag) => tags.appendChild(el("span", "", tag)));
  const actions = el("footer");
  actions.append(
    actionButton("打开", "", () => openDetail(entry.id)),
    actionButton("编辑", "", () => openEditor(entry.id)),
  );
  if (!statusIsSalvaged(entry)) {
    const salvageStatus = entry.type === "memory" ? "已打捞" : entry.type === "unfinished" ? "重新开始" : "仍可能抵达";
    actions.appendChild(actionButton("打捞", "highlight", () => updateEntryState(entry, salvageStatus, "这一段时间已经重新进入你的视野。")));
  }
  if (entry.type !== "memory" && !["重新开始", "仍可能抵达"].includes(entry.status)) {
    const restartStatus = entry.type === "unfinished" ? "重新开始" : "仍可能抵达";
    actions.appendChild(actionButton("重启", "", () => updateEntryState(entry, restartStatus, "状态已在本机更新；没有发送到其他产品。")));
  }
  actions.appendChild(actionButton("删除", "danger", () => deleteEntry(entry.id)));
  card.append(
    top,
    el("h4", "", entry.title),
    el("small", "oblivion-entry-date", dateLabel(entry)),
    el("p", "", entry.content.length > 170 ? `${entry.content.slice(0, 170)}…` : entry.content),
    tags,
    actions,
  );
  return card;
}

function renderList() {
  const list = $("oblivion-list");
  const entries = filteredEntries();
  list.replaceChildren();
  if (!entries.length) {
    const empty = el("div", "oblivion-empty");
    empty.append(el("i", "", "◷"), el("h3", "", state.entries.length ? "没有符合当前筛选的时间。" : "这里还没有被封存的时间。"), el("p", "", "有些事情不必等到被遗忘以后，才发现它曾经重要。"));
    empty.appendChild(actionButton(state.entries.length ? "清除筛选" : "封存第一段时间", "", () => {
      if (state.entries.length) {
        $("oblivion-search").value = "";
        $("oblivion-status-filter").value = "all";
        $("oblivion-time-filter").value = "all";
        if (state.view === "overview" || state.view === "chronicle") $("oblivion-type-filter").value = "all";
        renderList();
      } else openEditor();
    }));
    list.appendChild(empty);
    return;
  }
  entries.forEach((entry) => list.appendChild(renderEntryCard(entry)));
}

function renderAll() {
  updateStats();
  updateStatusFilter();
  renderList();
}

function hideTransientViews() {
  $("oblivion-editor").classList.add("hidden");
  $("oblivion-detail").classList.add("hidden");
  $("oblivion-import-panel").classList.add("hidden");
  $("oblivion-overview").classList.toggle("hidden", state.view !== "overview");
  $("oblivion-browser").classList.remove("hidden");
}

function setView(view) {
  state.view = view;
  document.querySelectorAll("[data-oblivion-view]").forEach((button) => button.classList.toggle("active", button.dataset.oblivionView === view));
  const type = ["memory", "unfinished", "lost_world"].includes(view) ? view : "all";
  $("oblivion-type-filter").value = type;
  $("oblivion-type-filter").disabled = type !== "all";
  $("oblivion-sort-field").classList.toggle("featured", view === "chronicle");
  hideTransientViews();
  renderAll();
}

function renderStatusOptions(type, selected = "") {
  const select = $("oblivion-status");
  select.replaceChildren(...TYPE_META[type].statuses.map((status) => new Option(status, status)));
  if (TYPE_META[type].statuses.includes(selected)) select.value = selected;
}

function openEditor(id = null) {
  const entry = id ? state.entries.find((item) => item.id === id) : null;
  state.editingId = entry?.id || null;
  $("oblivion-browser").classList.add("hidden");
  $("oblivion-overview").classList.add("hidden");
  $("oblivion-detail").classList.add("hidden");
  $("oblivion-import-panel").classList.add("hidden");
  $("oblivion-editor").classList.remove("hidden");
  $("oblivion-editor-title").textContent = entry ? "编辑时间片段" : "封存一段时间";
  $("oblivion-id").value = entry?.id || "";
  const type = entry?.type || "memory";
  document.querySelectorAll('input[name="oblivion-type"]').forEach((input) => { input.checked = input.value === type; });
  $("oblivion-title").value = entry?.title || "";
  $("oblivion-content").value = entry?.content || "";
  $("oblivion-date").value = entry?.occurred_at || "";
  $("oblivion-time-blurred").checked = Boolean(entry?.time_is_blurred);
  $("oblivion-date").disabled = Boolean(entry?.time_is_blurred);
  $("oblivion-tags").value = entry?.tags?.join("，") || "";
  $("oblivion-why").value = entry?.why_keep || "";
  $("oblivion-form-error").textContent = "";
  renderStatusOptions(type, entry?.status || "");
  window.setTimeout(() => $("oblivion-title").focus(), 60);
}

function saveEntry(event) {
  event.preventDefault();
  const type = document.querySelector('input[name="oblivion-type"]:checked')?.value || "memory";
  const tags = [...new Set($("oblivion-tags").value.split(/[,，]/).map((item) => item.trim()).filter(Boolean))];
  if (tags.length > 8) {
    $("oblivion-form-error").textContent = "标签最多 8 个。";
    return;
  }
  const existing = state.entries.find((item) => item.id === state.editingId);
  const now = new Date().toISOString();
  const entry = normalizeEntry({
    id: existing?.id || crypto.randomUUID(),
    type,
    title: $("oblivion-title").value,
    content: $("oblivion-content").value,
    occurred_at: $("oblivion-time-blurred").checked ? null : $("oblivion-date").value || null,
    time_is_blurred: $("oblivion-time-blurred").checked,
    tags,
    why_keep: $("oblivion-why").value,
    status: $("oblivion-status").value,
    created_at: existing?.created_at || now,
    updated_at: now,
  });
  if (!entry) {
    $("oblivion-form-error").textContent = "请填写标题与内容。";
    return;
  }
  state.entries = existing ? state.entries.map((item) => item.id === entry.id ? entry : item) : [entry, ...state.entries];
  persist();
  setView(state.view);
  state.notify(existing ? "时间片段已在本机更新。" : "这一段时间已封存到当前浏览器。");
}

function openDetail(id) {
  const entry = state.entries.find((item) => item.id === id);
  if (!entry) return;
  $("oblivion-browser").classList.add("hidden");
  $("oblivion-overview").classList.add("hidden");
  $("oblivion-editor").classList.add("hidden");
  const detail = $("oblivion-detail");
  detail.classList.remove("hidden");
  detail.replaceChildren();
  const header = el("header");
  const heading = el("div");
  heading.append(el("span", "overline", TYPE_META[entry.type].english), el("h3", "", entry.title), el("small", "", `${dateLabel(entry)} · ${entry.status}`));
  header.append(heading, actionButton("返回", "", () => setView(state.view)));
  const tags = el("div", "oblivion-entry-tags");
  entry.tags.forEach((tag) => tags.appendChild(el("span", "", tag)));
  const body = el("article");
  body.append(el("p", "", entry.content));
  if (entry.why_keep) body.append(el("h4", "", "为什么要留下"), el("p", "", entry.why_keep));
  body.appendChild(tags);
  const future = el("section", "oblivion-future-links");
  future.append(el("strong", "", "未来连接"), el("small", "", "正在形成"));
  ["送往造界", "交给光子魅影", "触发共振"].forEach((label) => {
    const button = el("button", "", label);
    button.type = "button";
    button.disabled = true;
    future.appendChild(button);
  });
  const actions = el("footer");
  actions.append(actionButton("编辑", "", () => openEditor(entry.id)), actionButton("删除", "danger", () => deleteEntry(entry.id)));
  detail.append(header, body, future, actions);
}

function deleteEntry(id) {
  const entry = state.entries.find((item) => item.id === id);
  if (!entry || !window.confirm(`确认删除“${entry.title}”吗？此操作只影响当前浏览器，且无法撤销。`)) return;
  state.entries = state.entries.filter((item) => item.id !== id);
  persist();
  setView(state.view);
  state.notify("时间片段已从当前浏览器删除。");
}

function exportArchive() {
  const payload = { version: 1, exported_at: new Date().toISOString(), entries: state.entries };
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `bingyan-oblivion-archive-${new Date().toISOString().slice(0, 10)}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  state.notify("史诗备份已导出。");
}

async function previewImport(file) {
  try {
    const parsed = JSON.parse(await file.text());
    const source = Array.isArray(parsed) ? parsed : parsed?.entries;
    if (!Array.isArray(source)) throw new Error("文件中没有条目数组。");
    const unique = new Map();
    source.forEach((item) => {
      const normalized = normalizeEntry(item);
      if (normalized && !unique.has(normalized.id)) unique.set(normalized.id, normalized);
    });
    if (!unique.size) throw new Error("没有找到结构有效的条目。");
    state.pendingImport = [...unique.values()];
    $("oblivion-import-count").textContent = String(state.pendingImport.length);
    $("oblivion-browser").classList.add("hidden");
    $("oblivion-overview").classList.add("hidden");
    $("oblivion-import-panel").classList.remove("hidden");
  } catch (error) {
    state.notify(`导入失败：${error.message}`);
  } finally {
    $("oblivion-import-file").value = "";
  }
}

function completeImport(mode) {
  if (!state.pendingImport) return;
  if (mode === "replace") {
    state.entries = state.pendingImport;
  } else {
    const existingIds = new Set(state.entries.map((entry) => entry.id));
    state.entries = [...state.entries, ...state.pendingImport.filter((entry) => !existingIds.has(entry.id))];
  }
  const count = state.pendingImport.length;
  state.pendingImport = null;
  persist();
  setView("overview");
  state.notify(`已${mode === "replace" ? "替换" : "合并"} ${count} 个有效条目。`);
}

export function openOblivionArchive() {
  const dialog = $("oblivion-archive-dialog");
  readEntries();
  setView(state.view || "overview");
  if (!dialog.open) dialog.showModal();
  window.requestAnimationFrame(() => { dialog.querySelector(".oblivion-archive-shell").scrollTop = 0; });
}

export function initOblivionArchive({ notify = () => {} } = {}) {
  if (state.initialized) return;
  state.initialized = true;
  state.notify = notify;
  readEntries();
  $("oblivion-close").addEventListener("click", () => $("oblivion-archive-dialog").close());
  $("oblivion-create").addEventListener("click", () => openEditor());
  $("oblivion-editor-cancel").addEventListener("click", () => setView(state.view));
  $("oblivion-form").addEventListener("submit", saveEntry);
  $("oblivion-time-blurred").addEventListener("change", () => {
    $("oblivion-date").disabled = $("oblivion-time-blurred").checked;
    if ($("oblivion-time-blurred").checked) $("oblivion-date").value = "";
  });
  document.querySelectorAll('input[name="oblivion-type"]').forEach((input) => input.addEventListener("change", () => renderStatusOptions(input.value)));
  document.querySelectorAll("[data-oblivion-view]").forEach((button) => button.addEventListener("click", () => setView(button.dataset.oblivionView)));
  ["oblivion-search", "oblivion-type-filter", "oblivion-status-filter", "oblivion-time-filter", "oblivion-sort"].forEach((id) => $(id).addEventListener(id === "oblivion-search" ? "input" : "change", renderList));
  $("oblivion-export").addEventListener("click", exportArchive);
  $("oblivion-import").addEventListener("click", () => $("oblivion-import-file").click());
  $("oblivion-import-file").addEventListener("change", (event) => event.target.files?.[0] && previewImport(event.target.files[0]));
  $("oblivion-import-cancel").addEventListener("click", () => { state.pendingImport = null; setView(state.view); });
  $("oblivion-import-merge").addEventListener("click", () => completeImport("merge"));
  $("oblivion-import-replace").addEventListener("click", () => completeImport("replace"));
  renderStatusOptions("memory");
  renderAll();
}

export const OBLIVION_STORAGE_KEY = STORAGE_KEY;
