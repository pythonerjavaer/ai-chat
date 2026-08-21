import DOMPurify from "dompurify";
import { marked } from "marked";
import { Capacitor } from "@capacitor/core";
import { Haptics, ImpactStyle } from "@capacitor/haptics";
import { Preferences } from "@capacitor/preferences";
import "./styles.css";

marked.setOptions({ gfm: true, breaks: true });

const configuredApiBase = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const API_BASE = configuredApiBase || "/api";
const STORAGE_KEYS = {
  token: "frostfire_token",
  workspace: "frostfire_workspace",
};
const WORKSPACE_ORDER = ["legal", "general", "finance"];
const WORKSPACE_META = {
  legal: { symbol: "§", eyebrow: "FROST LEGAL DESK", themeName: "寒冰工作台" },
  general: { symbol: "✦", eyebrow: "AURORA KNOWLEDGE DESK", themeName: "极光工作台" },
  finance: { symbol: "↗", eyebrow: "EMBER FINANCE DESK", themeName: "烈火工作台" },
};

const storage = {
  async get(key) {
    try {
      const result = await Preferences.get({ key });
      return result.value;
    } catch (_) {
      return localStorage.getItem(key);
    }
  },
  async set(key, value) {
    try { await Preferences.set({ key, value }); }
    catch (_) { localStorage.setItem(key, value); }
  },
  async remove(key) {
    try { await Preferences.remove({ key }); }
    catch (_) { localStorage.removeItem(key); }
  },
};

const state = {
  token: null,
  user: null,
  sessionId: null,
  sessions: [],
  documents: [],
  workspaces: [],
  workspace: "general",
  authMode: "login",
  sending: false,
  latestEvidence: { sources: [], tools: [] },
};

const $ = (id) => document.getElementById(id);
const elements = {
  authView: $("auth-view"), appView: $("app-view"), authForm: $("auth-form"),
  authKicker: $("auth-kicker"), authTitle: $("auth-title"), authDescription: $("auth-description"),
  authSubmit: $("auth-submit"), authError: $("auth-error"), authSwitch: $("auth-switch"),
  authSwitchCopy: $("auth-switch-copy"), privacyRow: $("privacy-row"),
  privacyAccepted: $("privacy-accepted"), username: $("username"), password: $("password"),
  workspaceTabs: $("workspace-tabs"), workspacePanelTitle: $("workspace-panel-title"),
  workspaceEyebrow: $("workspace-eyebrow"), workspaceTitle: $("workspace-title"),
  workspaceHeroCopy: $("workspace-hero-copy"), workspaceIndicator: $("workspace-indicator"),
  heroSymbol: $("hero-symbol"), lensLabel: $("lens-label"), lensScore: $("lens-score"),
  sessionList: $("session-list"), sessionCount: $("session-count"), documentList: $("document-list"),
  documentCount: $("document-count"), documentInput: $("document-input"), knowledgeTitle: $("knowledge-title"),
  uploadLabel: $("upload-label"), workspaceBoundary: $("workspace-boundary"), chatWindow: $("chat-window"),
  chatForm: $("chat-form"), messageInput: $("message-input"), sendButton: $("send-button"),
  conversationTitle: $("conversation-title"), composerHint: $("composer-hint"),
  conversationPanel: $("conversation-panel"), knowledgePanel: $("knowledge-panel"),
  panelBackdrop: $("panel-backdrop"), toast: $("toast"), networkStatus: $("network-status"),
  currentUsername: $("current-username"), avatar: $("avatar"), sidebarUsername: $("sidebar-username"),
  sidebarAvatar: $("sidebar-avatar"), settingsDialog: $("settings-dialog"),
  settingsUsername: $("settings-username"), settingsAvatar: $("settings-avatar"),
  deleteAccountForm: $("delete-account-form"), deletePassword: $("delete-password"),
  deleteConfirmation: $("delete-confirmation"), deleteError: $("delete-error"),
  consentDialog: $("consent-dialog"), evidenceProgress: $("evidence-progress"),
  evidencePercent: $("evidence-percent"), evidenceTitle: $("evidence-title"),
  evidenceSummary: $("evidence-summary"), evidenceDetail: $("evidence-detail"),
};

function activeWorkspace() {
  return state.workspaces.find((item) => item.id === state.workspace) || {
    id: "general",
    label: "通用文档",
    description: "围绕个人资料进行可追溯的问答与总结。",
    boundary: "请核对关键事实与来源。",
    hero: "把散落的信息，凝成可验证的洞见。",
    lens: "来源覆盖",
    quick_actions: [],
  };
}

async function haptic() {
  if (!Capacitor.isNativePlatform()) return;
  try { await Haptics.impact({ style: ImpactStyle.Light }); } catch (_) {}
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const response = await fetch(API_BASE + path, { ...options, headers });
  if (response.status === 401 && !path.startsWith("/auth/login")) await logout(false);
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function showToast(message, timeout = 3600) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => elements.toast.classList.add("hidden"), timeout);
}

function setAuthMode(mode) {
  state.authMode = mode;
  const registering = mode === "register";
  elements.authKicker.textContent = registering ? "创建私有空间" : "欢迎回来";
  elements.authTitle.textContent = registering ? "开启你的研究舱" : "进入你的研究舱";
  elements.authDescription.textContent = registering
    ? "账号用于隔离你的对话、文档与证据索引。"
    : "对话、文档与证据索引会与你的账号绑定。";
  elements.authSubmit.querySelector("span").textContent = registering ? "同意并创建账号" : "登录";
  elements.authSwitchCopy.textContent = registering ? "已经有账号？" : "还没有账号？";
  elements.authSwitch.textContent = registering ? "返回登录" : "创建账号";
  elements.privacyRow.classList.toggle("hidden", !registering);
  elements.privacyAccepted.required = registering;
  elements.password.autocomplete = registering ? "new-password" : "current-password";
  elements.authError.textContent = "";
}

async function authenticate(event) {
  event.preventDefault();
  elements.authError.textContent = "";
  if (!elements.authForm.reportValidity()) return;
  if (state.authMode === "register" && !elements.privacyAccepted.checked) {
    elements.authError.textContent = "创建账号前需要阅读并同意隐私政策。";
    return;
  }
  elements.authSubmit.disabled = true;
  elements.authSubmit.querySelector("span").textContent = state.authMode === "register" ? "正在创建…" : "正在验证…";
  try {
    const result = await api(`/auth/${state.authMode}`, {
      method: "POST",
      body: JSON.stringify({
        username: elements.username.value.trim(),
        password: elements.password.value,
        privacy_accepted: state.authMode === "register" && elements.privacyAccepted.checked,
      }),
    });
    state.token = result.access_token;
    state.user = result.user;
    await storage.set(STORAGE_KEYS.token, state.token);
    await haptic();
    await enterApp();
  } catch (error) {
    elements.authError.textContent = translateError(error.message);
  } finally {
    elements.authSubmit.disabled = false;
    elements.authSubmit.querySelector("span").textContent = state.authMode === "register" ? "同意并创建账号" : "登录";
  }
}

function translateError(message) {
  const known = {
    "Invalid username or password.": "用户名或密码不正确。",
    "Username already exists.": "这个用户名已经存在。",
    "Authentication required.": "请先登录。",
    "Password is incorrect.": "密码不正确。",
    'Enter "DELETE" to confirm.': "请输入 DELETE 确认。",
    "OpenAI API request failed.": "OpenAI API 暂时未能完成请求，请稍后重试。",
  };
  return known[message] || message;
}

async function enterApp() {
  elements.authView.classList.add("hidden");
  elements.appView.classList.remove("hidden");
  applyUser();
  await loadWorkspaces();
  await Promise.all([loadSessions(), loadDocuments()]);
  newConversation();
  if (!state.user.privacy_accepted && !elements.consentDialog.open) elements.consentDialog.showModal();
}

function applyUser() {
  const username = state.user?.username || "account";
  const initial = username.slice(0, 1).toUpperCase();
  [elements.currentUsername, elements.sidebarUsername, elements.settingsUsername].forEach((el) => { el.textContent = username; });
  [elements.avatar, elements.sidebarAvatar, elements.settingsAvatar].forEach((el) => { el.textContent = initial; });
}

async function logout(showMessage = true) {
  await storage.remove(STORAGE_KEYS.token);
  state.token = null;
  state.user = null;
  state.sessionId = null;
  state.sessions = [];
  state.documents = [];
  if (elements.settingsDialog.open) elements.settingsDialog.close();
  if (elements.consentDialog.open) elements.consentDialog.close();
  elements.appView.classList.add("hidden");
  elements.authView.classList.remove("hidden");
  elements.password.value = "";
  if (showMessage) showToast("已安全退出");
}

async function loadWorkspaces() {
  state.workspaces = await api("/workspaces");
  if (!state.workspaces.some((item) => item.id === state.workspace)) state.workspace = "general";
  renderWorkspaceTabs();
  renderWorkspaceChrome();
}

function renderWorkspaceTabs() {
  elements.workspaceTabs.replaceChildren();
  WORKSPACE_ORDER.forEach((workspaceId) => {
    const workspace = state.workspaces.find((item) => item.id === workspaceId);
    if (!workspace) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = `workspace-tab${workspaceId === state.workspace ? " active" : ""}`;
    button.dataset.workspace = workspaceId;
    button.title = WORKSPACE_META[workspaceId].themeName;
    const symbol = document.createElement("i");
    symbol.textContent = WORKSPACE_META[workspaceId].symbol;
    const label = document.createElement("span");
    label.textContent = workspace.label;
    button.append(symbol, label);
    button.addEventListener("click", () => changeWorkspace(workspaceId));
    elements.workspaceTabs.appendChild(button);
  });
}

function renderWorkspaceChrome() {
  const workspace = activeWorkspace();
  const meta = WORKSPACE_META[state.workspace] || WORKSPACE_META.general;
  document.body.dataset.workspace = state.workspace;
  document.querySelector('meta[name="theme-color"]').content = state.workspace === "legal" ? "#031521" : state.workspace === "finance" ? "#1a0b07" : "#07111f";
  elements.workspaceEyebrow.textContent = meta.eyebrow;
  elements.workspaceTitle.textContent = workspace.label;
  elements.workspacePanelTitle.textContent = `${workspace.label}记录`;
  elements.workspaceHeroCopy.textContent = workspace.hero || workspace.description;
  elements.workspaceIndicator.textContent = meta.themeName;
  elements.heroSymbol.textContent = meta.symbol;
  elements.lensLabel.textContent = workspace.lens || "来源覆盖";
  elements.knowledgeTitle.textContent = `${workspace.label}资料库`;
  elements.uploadLabel.textContent = `投放到${meta.themeName}`;
  elements.workspaceBoundary.textContent = `${workspace.boundary} 文档片段和消息会由 OpenAI API 处理。`;
  elements.messageInput.placeholder = state.workspace === "legal"
    ? "询问条款、义务、期限、风险或合规证据…"
    : state.workspace === "finance"
      ? "询问指标变化、计算口径、假设或风险…"
      : "发送消息，或询问已上传的资料…";
  elements.composerHint.textContent = `${workspace.boundary} 回答中的“证据透镜”表示来源覆盖，不代表结论必然正确。`;
  elements.evidenceTitle.textContent = workspace.lens || "证据透镜";
  document.querySelectorAll(".workspace-tab").forEach((button) => button.classList.toggle("active", button.dataset.workspace === state.workspace));
  updateEvidence(state.latestEvidence.sources, state.latestEvidence.tools);
}

async function changeWorkspace(workspaceId) {
  if (workspaceId === state.workspace) return;
  state.workspace = workspaceId;
  state.sessionId = null;
  state.latestEvidence = { sources: [], tools: [] };
  await storage.set(STORAGE_KEYS.workspace, workspaceId);
  await haptic();
  renderWorkspaceTabs();
  newConversation();
  try {
    await loadDocuments();
  } catch (error) {
    showToast(`资料库刷新失败：${translateError(error.message)}`);
  }
}

async function loadSessions() {
  state.sessions = await api("/sessions");
  renderSessions();
}

function renderSessions() {
  elements.sessionList.replaceChildren();
  const sessions = state.sessions.filter((session) => (session.workspace || "general") === state.workspace);
  elements.sessionCount.textContent = String(sessions.length);
  if (!sessions.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "当前工作台还没有研究记录。\n从一个问题开始。";
    elements.sessionList.appendChild(empty);
    return;
  }
  sessions.forEach((session) => {
    const row = document.createElement("div");
    row.className = `session-row${session.id === state.sessionId ? " active" : ""}`;
    const open = document.createElement("button");
    open.className = "session-open";
    open.type = "button";
    open.textContent = session.title;
    open.title = session.title;
    open.addEventListener("click", () => openSession(session));
    const remove = document.createElement("button");
    remove.className = "delete-button";
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "删除对话";
    remove.addEventListener("click", () => deleteSession(session.id));
    row.append(open, remove);
    elements.sessionList.appendChild(row);
  });
}

function renderWelcome() {
  const workspace = activeWorkspace();
  const meta = WORKSPACE_META[state.workspace] || WORKSPACE_META.general;
  elements.chatWindow.replaceChildren();
  const welcome = document.createElement("div");
  welcome.className = "welcome";
  const mark = document.createElement("div");
  mark.className = "welcome-mark";
  mark.textContent = meta.symbol;
  const title = document.createElement("h2");
  title.textContent = workspace.label;
  const description = document.createElement("p");
  description.textContent = `${workspace.description} ${workspace.boundary}`;
  const actions = document.createElement("div");
  actions.className = "quick-actions";
  (workspace.quick_actions || []).forEach((copy) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quick-action";
    button.textContent = copy;
    button.addEventListener("click", () => {
      elements.messageInput.value = copy;
      resizeComposer();
      elements.messageInput.focus();
    });
    actions.appendChild(button);
  });
  welcome.append(mark, title, description, actions);
  elements.chatWindow.appendChild(welcome);
}

function newConversation() {
  state.sessionId = null;
  state.latestEvidence = { sources: [], tools: [] };
  elements.conversationTitle.textContent = "新对话";
  renderWorkspaceChrome();
  renderSessions();
  renderWelcome();
  closePanels();
  if (window.innerWidth > 820) elements.messageInput.focus();
}

async function openSession(session) {
  try {
    const messages = await api(`/sessions/${session.id}/messages`);
    state.sessionId = session.id;
    state.workspace = session.workspace || "general";
    await storage.set(STORAGE_KEYS.workspace, state.workspace);
    renderWorkspaceChrome();
    renderWorkspaceTabs();
    await loadDocuments();
    elements.conversationTitle.textContent = session.title;
    elements.chatWindow.replaceChildren();
    messages.forEach((message) => appendMessage(message.role, message.content));
    renderSessions();
    closePanels();
    elements.chatWindow.scrollTop = elements.chatWindow.scrollHeight;
  } catch (error) { showToast(translateError(error.message)); }
}

async function deleteSession(sessionId) {
  if (!window.confirm("确定删除这个对话及其全部消息吗？")) return;
  try {
    await api(`/sessions/${sessionId}`, { method: "DELETE" });
    if (state.sessionId === sessionId) newConversation();
    await loadSessions();
    showToast("对话已删除");
  } catch (error) { showToast(translateError(error.message)); }
}

async function loadDocuments() {
  const requestedWorkspace = state.workspace;
  const documents = await api(`/documents?workspace=${encodeURIComponent(requestedWorkspace)}`);
  if (requestedWorkspace !== state.workspace) return;
  state.documents = documents;
  renderDocuments();
}

function renderDocuments() {
  elements.documentList.replaceChildren();
  elements.documentCount.textContent = String(state.documents.length);
  if (!state.documents.length) {
    const empty = document.createElement("div");
    empty.className = "empty-list";
    empty.textContent = "还没有资料。上传后，回答会显示命中的文件与页码。";
    elements.documentList.appendChild(empty);
    elements.lensScore.textContent = "等待资料";
    return;
  }
  if (!state.latestEvidence.sources.length) elements.lensScore.textContent = `${state.documents.length} 份资料就绪`;
  state.documents.forEach((documentItem) => {
    const row = document.createElement("div");
    row.className = "document-row";
    const icon = document.createElement("span");
    icon.className = "document-icon";
    icon.textContent = documentItem.file_type || "DOC";
    const copy = document.createElement("div");
    copy.className = "document-copy";
    const name = document.createElement("span");
    name.className = "document-name";
    name.textContent = documentItem.name;
    name.title = documentItem.name;
    const detail = document.createElement("small");
    detail.textContent = `${documentItem.chunk_count} 个证据片段`;
    copy.append(name, detail);
    const remove = document.createElement("button");
    remove.className = "delete-button";
    remove.type = "button";
    remove.textContent = "×";
    remove.title = "移除资料";
    remove.addEventListener("click", () => deleteDocument(documentItem.id));
    row.append(icon, copy, remove);
    elements.documentList.appendChild(row);
  });
}

async function uploadDocument() {
  const file = elements.documentInput.files[0];
  if (!file) return;
  if (file.size > 10_000_000) {
    showToast("文件超过 10 MB，请压缩或拆分后上传。", 5000);
    elements.documentInput.value = "";
    return;
  }
  const data = new FormData();
  data.append("file", file);
  data.append("workspace", state.workspace);
  try {
    elements.uploadLabel.textContent = "正在提取并建立索引…";
    showToast(`正在处理 ${file.name}，请保持页面开启…`, 8000);
    await api("/documents", { method: "POST", body: data });
    await loadDocuments();
    await haptic();
    showToast(`已加入${WORKSPACE_META[state.workspace].themeName}`);
  } catch (error) {
    showToast(translateError(error.message), 5200);
  } finally {
    elements.documentInput.value = "";
    elements.uploadLabel.textContent = `投放到${WORKSPACE_META[state.workspace].themeName}`;
  }
}

async function deleteDocument(documentId) {
  if (!window.confirm("确定从当前资料库移除这个文件及其索引吗？")) return;
  try {
    await api(`/documents/${documentId}`, { method: "DELETE" });
    await loadDocuments();
    showToast("资料与索引已删除");
  } catch (error) { showToast(translateError(error.message)); }
}

function safeMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ""), {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "iframe", "form", "input", "button"],
    FORBID_ATTR: ["style", "onerror", "onclick"],
  });
}

function appendMessage(role, text, pending = false) {
  elements.chatWindow.querySelector(".welcome")?.remove();
  const wrapper = document.createElement("div");
  wrapper.className = `message ${role === "user" ? "user" : "assistant"}`;
  const avatar = document.createElement("div");
  avatar.className = "message-avatar";
  avatar.textContent = role === "user" ? "YOU" : WORKSPACE_META[state.workspace].symbol;
  const body = document.createElement("div");
  body.className = `message-body${pending ? " typing" : ""}`;
  if (role === "assistant") body.innerHTML = safeMarkdown(text);
  else body.textContent = text;
  wrapper.append(avatar, body);
  elements.chatWindow.appendChild(wrapper);
  elements.chatWindow.scrollTop = elements.chatWindow.scrollHeight;
  return { wrapper, body, text: text || "" };
}

function appendTags(sources, tools) {
  if (!sources.length && !tools.length) return;
  const meta = document.createElement("div");
  meta.className = "message-meta";
  const seen = new Set();
  sources.forEach((source) => {
    const label = `来源 · ${source.name}${source.page ? ` · 第 ${source.page} 页` : ""}`;
    if (seen.has(label)) return;
    seen.add(label);
    const tag = document.createElement("span");
    tag.className = "tag source";
    tag.textContent = label;
    tag.title = source.score ? `检索相似度 ${Math.round(source.score * 100)}%` : label;
    meta.appendChild(tag);
  });
  [...new Set(tools)].forEach((name) => {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = `工具 · ${humanizeTool(name)}`;
    meta.appendChild(tag);
  });
  elements.chatWindow.appendChild(meta);
}

function humanizeTool(name) {
  return ({ calculate: "安全计算", get_current_time: "时间", calculate_financial_metric: "金融指标计算" })[name] || name;
}

function updateEvidence(sources = [], tools = []) {
  state.latestEvidence = { sources, tools };
  const uniqueSources = [...new Map(sources.map((source) => [`${source.document_id}-${source.page || ""}`, source])).values()];
  const average = uniqueSources.length
    ? uniqueSources.reduce((sum, source) => sum + Math.max(0, Math.min(1, Number(source.score) || 0)), 0) / uniqueSources.length
    : 0;
  const percent = uniqueSources.length ? Math.max(30, Math.round(average * 100)) : 0;
  elements.evidenceProgress.style.strokeDashoffset = String(100 - percent);
  elements.evidencePercent.textContent = String(percent);
  if (uniqueSources.length) {
    elements.evidenceSummary.textContent = `${uniqueSources.length} 个来源片段命中`;
    elements.evidenceDetail.textContent = `${new Set(uniqueSources.map((source) => source.name)).size} 份文件 · ${new Set(tools).size} 个工具。百分比表示检索相关度，不是准确率。`;
    elements.lensScore.textContent = `${uniqueSources.length} 个来源 · ${percent}% 相关`;
  } else if (tools.length) {
    elements.evidenceSummary.textContent = "本次使用计算工具";
    elements.evidenceDetail.textContent = `已调用：${[...new Set(tools)].map(humanizeTool).join("、")}。未命中资料来源。`;
    elements.lensScore.textContent = "工具已核算";
  } else {
    elements.evidenceSummary.textContent = "等待第一次分析";
    elements.evidenceDetail.textContent = state.documents.length ? "提出与资料相关的问题后，这里会显示命中的来源与工具。" : "先上传资料，系统会显示回答采用的来源与工具。";
    elements.lensScore.textContent = state.documents.length ? `${state.documents.length} 份资料就绪` : "等待资料";
  }
}

function parseSseBlock(block) {
  let event = "message";
  const data = [];
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  });
  return { event, data: data.length ? JSON.parse(data.join("\n")) : {} };
}

async function sendMessage(event) {
  event.preventDefault();
  const message = elements.messageInput.value.trim();
  if (!message || state.sending) return;
  if (!navigator.onLine) {
    showToast("当前处于离线状态，恢复网络后再试。", 5000);
    return;
  }
  state.sending = true;
  elements.sendButton.disabled = true;
  elements.messageInput.value = "";
  resizeComposer();
  appendMessage("user", message);
  const pending = appendMessage("assistant", "", true);
  const sources = [];
  const tools = [];
  let receivedText = false;

  try {
    const response = await fetch(API_BASE + "/chat/stream", {
      method: "POST",
      headers: { Authorization: `Bearer ${state.token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ message, session_id: state.sessionId, workspace: state.workspace }),
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    if (!response.body) throw new Error("当前环境不支持流式读取。");

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done }).replace(/\r\n/g, "\n");
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) >= 0) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        if (!block.trim()) continue;
        const packet = parseSseBlock(block);
        if (packet.event === "meta") {
          state.sessionId = packet.data.session_id;
          sources.push(...(packet.data.sources || []));
          updateEvidence(sources, tools);
        } else if (packet.event === "token") {
          if (!receivedText) {
            pending.body.classList.remove("typing");
            receivedText = true;
          }
          pending.text += packet.data.content;
          pending.body.innerHTML = safeMarkdown(pending.text);
          elements.chatWindow.scrollTop = elements.chatWindow.scrollHeight;
        } else if (packet.event === "tool") {
          tools.push(packet.data.name);
          updateEvidence(sources, tools);
        } else if (packet.event === "done") {
          tools.push(...(packet.data.tools_used || []));
        } else if (packet.event === "error") {
          throw new Error(packet.data.detail || "流式请求失败");
        }
      }
      if (done) break;
    }
    pending.body.classList.remove("typing");
    if (!pending.text) pending.body.textContent = "模型没有返回文本。";
    appendTags(sources, tools);
    updateEvidence(sources, tools);
    await loadSessions();
    const active = state.sessions.find((item) => item.id === state.sessionId);
    if (active) elements.conversationTitle.textContent = active.title;
    await haptic();
  } catch (error) {
    pending.body.classList.remove("typing");
    pending.body.textContent = `请求失败：${translateError(error.message)}`;
  } finally {
    state.sending = false;
    elements.sendButton.disabled = false;
    if (window.innerWidth > 820) elements.messageInput.focus();
  }
}

function resizeComposer() {
  elements.messageInput.style.height = "auto";
  elements.messageInput.style.height = `${Math.min(elements.messageInput.scrollHeight, 150)}px`;
}

function openPanel(panel) {
  closePanels();
  panel.classList.add("open");
  elements.panelBackdrop.classList.remove("hidden");
}

function closePanels() {
  elements.conversationPanel.classList.remove("open");
  elements.knowledgePanel.classList.remove("open");
  elements.panelBackdrop.classList.add("hidden");
}

function openSettings() {
  closePanels();
  elements.deleteAccountForm.classList.add("hidden");
  elements.deletePassword.value = "";
  elements.deleteConfirmation.value = "";
  elements.deleteError.textContent = "";
  elements.settingsDialog.showModal();
}

async function deleteAccount() {
  elements.deleteError.textContent = "";
  if (!elements.deletePassword.value || elements.deleteConfirmation.value !== "DELETE") {
    elements.deleteError.textContent = "请输入当前密码，并输入 DELETE 确认。";
    return;
  }
  if (!window.confirm("最后确认：永久删除账号和全部数据？此操作无法恢复。")) return;
  try {
    await api("/auth/account", {
      method: "DELETE",
      body: JSON.stringify({ password: elements.deletePassword.value, confirmation: elements.deleteConfirmation.value }),
    });
    await logout(false);
    showToast("账号、对话与文档已永久删除", 5000);
  } catch (error) { elements.deleteError.textContent = translateError(error.message); }
}

async function acceptPrivacyConsent() {
  try {
    state.user = await api("/auth/privacy-consent", { method: "POST", body: JSON.stringify({ accepted: true }) });
    elements.consentDialog.close();
    showToast("隐私选择已记录，你可以开始使用。", 4500);
  } catch (error) { showToast(translateError(error.message), 5000); }
}

function updateNetwork() {
  const online = navigator.onLine;
  elements.networkStatus.classList.toggle("offline", !online);
  elements.networkStatus.querySelector("b").textContent = online ? "在线" : "离线";
}

elements.authForm.addEventListener("submit", authenticate);
elements.authSwitch.addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
$("new-chat").addEventListener("click", newConversation);
$("brand-home").addEventListener("click", (event) => { event.preventDefault(); newConversation(); });
elements.documentInput.addEventListener("change", uploadDocument);
elements.chatForm.addEventListener("submit", sendMessage);
elements.messageInput.addEventListener("input", resizeComposer);
elements.messageInput.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
    event.preventDefault();
    elements.chatForm.requestSubmit();
  }
});
$("composer-upload").addEventListener("click", () => elements.documentInput.click());
$("mobile-menu").addEventListener("click", () => openPanel(elements.conversationPanel));
$("panel-close").addEventListener("click", closePanels);
$("knowledge-toggle").addEventListener("click", () => openPanel(elements.knowledgePanel));
$("knowledge-close").addEventListener("click", closePanels);
$("mobile-knowledge").addEventListener("click", () => openPanel(elements.knowledgePanel));
$("open-evidence").addEventListener("click", () => openPanel(elements.knowledgePanel));
elements.panelBackdrop.addEventListener("click", closePanels);
$("settings-button").addEventListener("click", openSettings);
$("sidebar-settings").addEventListener("click", openSettings);
$("logout-button").addEventListener("click", () => logout());
$("show-delete-account").addEventListener("click", () => elements.deleteAccountForm.classList.toggle("hidden"));
$("confirm-delete-account").addEventListener("click", deleteAccount);
$("accept-consent").addEventListener("click", acceptPrivacyConsent);
$("consent-logout").addEventListener("click", () => logout());
window.addEventListener("online", updateNetwork);
window.addEventListener("offline", updateNetwork);
window.addEventListener("resize", () => { if (window.innerWidth > 1180) closePanels(); });

(async function bootstrap() {
  updateNetwork();
  state.token = await storage.get(STORAGE_KEYS.token);
  state.workspace = (await storage.get(STORAGE_KEYS.workspace)) || "general";
  if (Capacitor.isNativePlatform() && !configuredApiBase) {
    elements.authError.textContent = "移动端构建尚未配置正式 HTTPS API 地址。";
  }
  if (!state.token) return;
  try {
    state.user = await api("/auth/me");
    await enterApp();
  } catch (_) { await logout(false); }
})();
