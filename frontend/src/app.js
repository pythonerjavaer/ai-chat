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
  crossExamDocuments: { legal: [], finance: [] },
  crossExamRunning: false,
  spaceTemplates: [],
  spaces: [],
  billing: null,
  selectedTemplateId: "project_engineer",
  activeSpaceId: null,
  studioRunning: false,
  recruitmentProfile: null,
  recruitmentJobs: [],
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
  crossExamDialog: $("cross-exam-dialog"), crossExamFocus: $("cross-focus"),
  crossExamRun: $("cross-exam-run"), crossExamResults: $("cross-exam-results"),
  crossExamError: $("cross-exam-error"), crossLegalCount: $("cross-legal-count"),
  crossFinanceCount: $("cross-finance-count"),
  studioDialog: $("studio-dialog"), studioTemplateGrid: $("space-template-grid"),
  spaceForm: $("space-form"), spaceName: $("space-name"), spaceDescription: $("space-description"),
  spaceRules: $("space-rules"), spaceBudget: $("space-budget"), spaceCreate: $("space-create"),
  spaceFormError: $("space-form-error"), spaceList: $("space-list"), spaceCount: $("space-count"),
  billingPlan: $("billing-plan"), billingUsage: $("billing-usage"), spaceRunner: $("space-runner"),
  runnerIcon: $("runner-icon"), runnerName: $("runner-name"), runnerBudget: $("runner-budget"),
  runnerInput: $("runner-input"), runnerSend: $("runner-send"), runnerOutput: $("runner-output"),
  recruitmentDialog: $("recruitment-dialog"), recruitmentForm: $("recruitment-form"),
  recruitmentRoles: $("recruitment-roles"), recruitmentIndustries: $("recruitment-industries"),
  recruitmentLocations: $("recruitment-locations"), recruitmentBackground: $("recruitment-background"),
  recruitmentStart: $("recruitment-start"), recruitmentEnd: $("recruitment-end"),
  recruitmentJobs: $("recruitment-jobs"), recruitmentStatus: $("recruitment-source-status"),
  recruitmentRefresh: $("recruitment-refresh"),
  recruitmentError: $("recruitment-error"),
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
    "OpenAI API could not complete the cross-examination.": "OpenAI API 暂时未能完成冰火交叉审查，请稍后重试。",
    "Cross-examination requires at least one document in both the legal and finance workspaces.": "请先在寒冰工作台和烈火工作台各上传至少一份资料。",
    "Your current plan has reached its AI Space limit.": "当前方案已达到 AI Space 数量上限。",
    "The requested Space token budget exceeds your plan limit.": "这个 Space 的 Token 上限超过当前方案允许范围。",
    "This AI Space has reached its monthly Token budget.": "这个 AI Space 已达到本月 Token 上限。",
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

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = String(text);
  return element;
}

function setCrossExamCounts() {
  const legalCount = state.crossExamDocuments.legal.length;
  const financeCount = state.crossExamDocuments.finance.length;
  elements.crossLegalCount.textContent = `${legalCount} 份资料`;
  elements.crossFinanceCount.textContent = `${financeCount} 份资料`;
  elements.crossExamRun.disabled = state.crossExamRunning || !legalCount || !financeCount;
  if (!legalCount || !financeCount) {
    elements.crossExamError.textContent = "请先在寒冰工作台和烈火工作台各上传至少一份资料。";
  }
}

async function openCrossExam() {
  closePanels();
  elements.crossExamError.textContent = "";
  if (!elements.crossExamDialog.open) elements.crossExamDialog.showModal();
  elements.crossLegalCount.textContent = "检查中…";
  elements.crossFinanceCount.textContent = "检查中…";
  elements.crossExamRun.disabled = true;
  try {
    const [legal, finance] = await Promise.all([
      api("/documents?workspace=legal"),
      api("/documents?workspace=finance"),
    ]);
    state.crossExamDocuments = { legal, finance };
    setCrossExamCounts();
  } catch (error) {
    elements.crossExamError.textContent = translateError(error.message);
  }
}

function renderCrossExamLoading() {
  elements.crossExamResults.replaceChildren();
  const loading = makeElement("div", "cross-loading");
  loading.append(
    makeElement("div", "cross-loading-orbit"),
    makeElement("strong", "", "正在让条款与数字相互质询…"),
    makeElement("p", "", "建立证据锁链 · 推演三档情景 · 标记未知事项"),
  );
  elements.crossExamResults.appendChild(loading);
}

function resultSection(title, note) {
  const heading = makeElement("div", "result-section-title");
  heading.append(makeElement("h4", "", title), makeElement("span", "", note));
  return heading;
}

function renderCrossExamResult(result) {
  elements.crossExamResults.replaceChildren();

  const header = makeElement("div", "cross-result-header");
  const summary = makeElement("div");
  summary.append(
    makeElement("span", "overline", "CROSS-EXAM VERDICT"),
    makeElement("h3", "", result.headline || "冰火交叉审查结果"),
    makeElement("p", "", result.executive_summary || ""),
  );
  const passport = makeElement("div", "analysis-passport");
  passport.append(
    makeElement("small", "", "ANALYSIS PASSPORT"),
    makeElement("strong", "", result.analysis_id || "—"),
    makeElement("small", "", "相同资料与焦点可用此指纹核对输入版本"),
  );
  header.append(summary, passport);
  elements.crossExamResults.appendChild(header);

  const collisions = result.collisions || [];
  elements.crossExamResults.appendChild(resultSection("因果碰撞卡", `${collisions.length} 条跨域链路`));
  const collisionGrid = makeElement("div", "collision-grid");
  const severityLabels = { critical: "临界", high: "高", medium: "中", low: "低" };
  collisions.forEach((collision) => {
    const card = makeElement("article", "collision-card");
    card.dataset.severity = collision.severity || "medium";
    const top = makeElement("div", "collision-top");
    top.append(
      makeElement("h5", "", collision.title),
      makeElement("span", "severity-pill", `${severityLabels[collision.severity] || "中"}风险`),
    );
    const confidence = Math.max(0, Math.min(100, Number(collision.confidence) || 0));
    const meter = makeElement("div", "confidence-meter");
    const line = makeElement("i");
    line.style.setProperty("--confidence", `${confidence}%`);
    meter.append(makeElement("span", "", "证据覆盖"), line, makeElement("b", "", `${confidence}%`));

    const chain = makeElement("div", "causal-chain");
    const legalNode = makeElement("div", "causal-node frost");
    legalNode.append(makeElement("small", "", "寒冰 · 条款机制"), document.createTextNode(collision.legal_mechanism || "—"));
    const financeNode = makeElement("div", "causal-node ember");
    financeNode.append(makeElement("small", "", "烈火 · 财务后果"), document.createTextNode(collision.financial_consequence || "—"));
    chain.append(legalNode, makeElement("div", "causal-arrow", "→"), financeNode);

    const evidence = makeElement("div", "evidence-chain");
    (collision.evidence || []).forEach((source) => {
      const chip = makeElement(
        "span",
        `source-chip ${source.workspace === "legal" ? "frost" : "ember"}`,
        `${source.source_id} · ${source.name}${source.page ? ` · P${source.page}` : ""}`,
      );
      chip.title = source.excerpt || source.name;
      evidence.appendChild(chip);
    });

    const gapAction = makeElement("div", "gap-action");
    const gap = makeElement("div", "gap-box");
    gap.append(makeElement("b", "", "证据缺口"), document.createTextNode(collision.missing_evidence || "未标记"));
    const action = makeElement("div", "action-box");
    action.append(makeElement("b", "", "下一步"), document.createTextNode(collision.next_action || "继续核对"));
    gapAction.append(gap, action);

    card.append(
      top,
      meter,
      chain,
      makeElement("p", "collision-why", collision.why_it_matters || ""),
      evidence,
      gapAction,
    );
    collisionGrid.appendChild(card);
  });
  elements.crossExamResults.appendChild(collisionGrid);

  const scenarios = result.stress_scenarios || [];
  elements.crossExamResults.appendChild(resultSection("反事实压力舱", "情景不是预测，不虚构数值"));
  const scenarioGrid = makeElement("div", "scenario-grid");
  scenarios.forEach((scenario) => {
    const card = makeElement("article", "scenario-card");
    card.appendChild(makeElement("h5", "", scenario.name));
    const list = makeElement("dl");
    [
      ["触发条件", scenario.trigger],
      ["影响链", scenario.impact_chain],
      ["早期信号", scenario.early_warning],
      ["响应动作", scenario.response],
    ].forEach(([label, value]) => {
      const row = makeElement("div");
      row.append(makeElement("dt", "", label), makeElement("dd", "", value || "—"));
      list.appendChild(row);
    });
    card.appendChild(list);
    scenarioGrid.appendChild(card);
  });
  elements.crossExamResults.appendChild(scenarioGrid);

  const blindSpots = result.blind_spots || [];
  elements.crossExamResults.appendChild(resultSection("未知事项雷达", "主动显示模型无法从资料确认的内容"));
  const blindList = makeElement("ul", "blind-list");
  blindSpots.forEach((item) => blindList.appendChild(makeElement("li", "", item)));
  if (!blindSpots.length) blindList.appendChild(makeElement("li", "", "本次未返回额外未知事项，仍需人工复核关键结论。"));
  elements.crossExamResults.appendChild(blindList);

  const sources = result.sources || [];
  elements.crossExamResults.appendChild(resultSection("证据锁链", `${sources.length} 个锁定片段`));
  const ledger = makeElement("div", "source-ledger");
  sources.forEach((source) => {
    const row = makeElement("div", "source-ledger-row");
    row.dataset.workspace = source.workspace;
    row.title = source.excerpt || source.name;
    row.append(
      makeElement("b", "", source.source_id),
      makeElement("span", "", `${source.name}${source.page ? ` · 第 ${source.page} 页` : ""}`),
      makeElement("small", "", `${Math.round((Number(source.score) || 0) * 100)}% 相关`),
    );
    ledger.appendChild(row);
  });
  elements.crossExamResults.appendChild(ledger);
}

async function runCrossExam() {
  if (state.crossExamRunning) return;
  const focus = elements.crossExamFocus.value.trim();
  if (focus.length < 4) {
    elements.crossExamError.textContent = "请写下至少 4 个字的审查焦点。";
    return;
  }
  state.crossExamRunning = true;
  elements.crossExamError.textContent = "";
  elements.crossExamRun.disabled = true;
  elements.crossExamRun.querySelector("span").textContent = "双域质询进行中…";
  renderCrossExamLoading();
  try {
    const result = await api("/cross-exam", {
      method: "POST",
      body: JSON.stringify({ focus }),
    });
    renderCrossExamResult(result);
    await haptic();
  } catch (error) {
    elements.crossExamError.textContent = translateError(error.message);
    const failed = makeElement("div", "cross-empty-state");
    failed.append(
      makeElement("i", "", "!"),
      makeElement("h3", "", "本次交叉审查未完成"),
      makeElement("p", "", translateError(error.message)),
    );
    elements.crossExamResults.replaceChildren(failed);
  } finally {
    state.crossExamRunning = false;
    elements.crossExamRun.querySelector("span").textContent = "启动双域质询";
    setCrossExamCounts();
  }
}

const SPACE_GLOWS = {
  forge: "#ff8150",
  aurora: "#8d7dff",
  frost: "#45d8ff",
  ember: "#ff7a31",
  mono: "#b7c5d5",
};

function selectedSpaceTemplate() {
  return state.spaceTemplates.find((item) => item.id === state.selectedTemplateId)
    || state.spaceTemplates[0];
}

function renderSpaceTemplates() {
  elements.studioTemplateGrid.replaceChildren();
  state.spaceTemplates.forEach((template) => {
    const button = makeElement(
      "button",
      `space-template${template.id === state.selectedTemplateId ? " active" : ""}`,
    );
    button.type = "button";
    button.append(
      makeElement("span", "", template.icon),
      makeElement("strong", "", template.label),
      makeElement("small", "", template.description),
    );
    button.addEventListener("click", () => {
      state.selectedTemplateId = template.id;
      elements.spaceRules.value = template.system_prompt;
      if (!elements.spaceDescription.value.trim()) elements.spaceDescription.value = template.description;
      renderSpaceTemplates();
    });
    elements.studioTemplateGrid.appendChild(button);
  });
}

function renderBilling() {
  const billing = state.billing;
  if (!billing) return;
  const plan = billing.plan === "pro" ? "Pro" : "Free";
  const used = billing.usage?.total_tokens || 0;
  const total = billing.limits?.monthly_tokens || 0;
  elements.billingPlan.textContent = `${plan} · ${billing.space_count}/${billing.limits?.max_spaces || 0} Spaces`;
  elements.billingUsage.textContent = `${used.toLocaleString()} / ${total.toLocaleString()} Tokens · ${billing.period}`;
  const spaceLimit = billing.limits?.max_space_tokens || 10_000;
  [...elements.spaceBudget.options].forEach((option) => {
    option.disabled = Number(option.value) > spaceLimit;
  });
}

function renderSpaces() {
  elements.spaceList.replaceChildren();
  elements.spaceCount.textContent = String(state.spaces.length);
  if (!state.spaces.length) {
    elements.spaceList.appendChild(makeElement(
      "div",
      "space-empty",
      "还没有 AI Space。选择左侧模板，写下你的规则，就能创建一个可运行的专属智能体。",
    ));
    return;
  }
  state.spaces.forEach((space) => {
    const card = makeElement(
      "button",
      `space-card${space.id === state.activeSpaceId ? " active" : ""}`,
    );
    card.type = "button";
    card.style.setProperty("--space-glow", SPACE_GLOWS[space.theme] || SPACE_GLOWS.mono);
    const top = makeElement("div", "space-card-top");
    top.append(
      makeElement("span", "space-card-icon", space.icon),
      (() => {
        const copy = makeElement("div");
        copy.append(makeElement("strong", "", space.name), makeElement("small", "", space.template_id.replaceAll("_", " · ")));
        return copy;
      })(),
    );
    const used = space.usage?.total_tokens || 0;
    const budget = space.monthly_token_budget || 0;
    const foot = makeElement("div", "space-card-foot");
    foot.append(makeElement("span", "", `已用 ${used.toLocaleString()} Tokens`), makeElement("span", "", `上限 ${budget.toLocaleString()}`));
    card.append(top, makeElement("p", "", space.description), foot);
    card.addEventListener("click", () => selectSpace(space.id));
    elements.spaceList.appendChild(card);
  });
}

function selectSpace(spaceId, preserveOutput = false) {
  const space = state.spaces.find((item) => item.id === spaceId);
  if (!space) return;
  state.activeSpaceId = spaceId;
  elements.spaceRunner.classList.remove("hidden");
  elements.runnerIcon.textContent = space.icon;
  elements.runnerName.textContent = space.name;
  const used = space.usage?.total_tokens || 0;
  elements.runnerBudget.textContent = `${used.toLocaleString()} / ${space.monthly_token_budget.toLocaleString()} Tokens`;
  if (!preserveOutput) {
    elements.runnerOutput.textContent = "给这个 Space 一项任务。它会遵守你刚才定义的规则，并返回本次实际 Token 消耗。";
  }
  renderSpaces();
  elements.runnerInput.focus();
}

async function refreshStudio() {
  const [templates, spaces, billing] = await Promise.all([
    api("/platform/templates"),
    api("/spaces"),
    api("/billing/status"),
  ]);
  state.spaceTemplates = templates;
  state.spaces = spaces;
  state.billing = billing;
  if (!state.spaceTemplates.some((item) => item.id === state.selectedTemplateId)) {
    state.selectedTemplateId = state.spaceTemplates[0]?.id || "blank";
  }
  renderSpaceTemplates();
  renderBilling();
  renderSpaces();
}

async function openStudio() {
  closePanels();
  elements.spaceFormError.textContent = "";
  if (!elements.studioDialog.open) elements.studioDialog.showModal();
  try {
    await refreshStudio();
    const template = selectedSpaceTemplate();
    if (template && !elements.spaceRules.value.trim()) {
      elements.spaceRules.value = template.system_prompt;
      elements.spaceDescription.value = template.description;
    }
  } catch (error) {
    elements.spaceFormError.textContent = translateError(error.message);
  }
}

async function createSpace(event) {
  event.preventDefault();
  elements.spaceFormError.textContent = "";
  if (!elements.spaceForm.reportValidity()) return;
  const template = selectedSpaceTemplate();
  if (!template) return;
  elements.spaceCreate.disabled = true;
  elements.spaceCreate.querySelector("span").textContent = "正在创建…";
  try {
    const created = await api("/spaces", {
      method: "POST",
      body: JSON.stringify({
        name: elements.spaceName.value.trim(),
        description: elements.spaceDescription.value.trim(),
        template_id: template.id,
        system_prompt: elements.spaceRules.value.trim(),
        icon: template.icon,
        theme: template.theme,
        monthly_token_budget: Number(elements.spaceBudget.value),
      }),
    });
    elements.spaceName.value = "";
    elements.spaceDescription.value = "";
    elements.spaceRules.value = template.system_prompt;
    await refreshStudio();
    selectSpace(created.id);
    showToast("AI Space 已创建，现在可以运行第一项任务。", 5000);
    await haptic();
  } catch (error) {
    elements.spaceFormError.textContent = translateError(error.message);
  } finally {
    elements.spaceCreate.disabled = false;
    elements.spaceCreate.querySelector("span").textContent = "创建可运行的 AI Space";
  }
}

async function runActiveSpace() {
  const message = elements.runnerInput.value.trim();
  if (!state.activeSpaceId || !message || state.studioRunning) return;
  state.studioRunning = true;
  elements.runnerSend.disabled = true;
  elements.runnerSend.querySelector("span").textContent = "正在运行…";
  elements.runnerOutput.textContent = "模型正在遵守 Space 规则完成任务…";
  try {
    const result = await api(`/spaces/${encodeURIComponent(state.activeSpaceId)}/run`, {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    elements.runnerOutput.innerHTML = safeMarkdown(result.reply || "模型没有返回文本。");
    const usage = result.usage || {};
    const footer = makeElement("div", "");
    footer.textContent = `本次消耗：输入 ${usage.input_tokens || 0} · 输出 ${usage.output_tokens || 0} · 合计 ${usage.total_tokens || 0} Tokens`;
    elements.runnerOutput.appendChild(footer);
    elements.runnerInput.value = "";
    state.billing = result.billing;
    await refreshStudio();
    selectSpace(state.activeSpaceId, true);
    await haptic();
  } catch (error) {
    elements.runnerOutput.textContent = `运行失败：${translateError(error.message)}`;
  } finally {
    state.studioRunning = false;
    elements.runnerSend.disabled = false;
    elements.runnerSend.querySelector("span").textContent = "运行一次";
  }
}

function explainBillingSetup() {
  const message = state.billing?.apple_store?.message
    || "App Store Connect 商品和服务器验签尚未配置。";
  showToast(`Pro 订阅尚未启用：${message}`, 7000);
}

function splitRecruitmentValues(value) {
  return value.split(/[，,、]/).map((item) => item.trim()).filter(Boolean).slice(0, 12);
}

function renderRecruitmentProfile(profile) {
  if (!profile) return;
  elements.recruitmentRoles.value = (profile.desired_roles || []).join("，");
  elements.recruitmentIndustries.value = (profile.industries || []).join("，");
  elements.recruitmentLocations.value = (profile.locations || []).join("，");
  elements.recruitmentBackground.value = profile.background || "";
  elements.recruitmentStart.value = profile.availability_start || "";
  elements.recruitmentEnd.value = profile.availability_end || "";
  document.querySelectorAll(".recruitment-checks input").forEach((input) => {
    input.checked = (profile.employer_types || []).includes(input.value);
  });
  document.querySelectorAll("[data-choice-group]").forEach((group) => {
    const key = group.dataset.choiceGroup;
    const values = Array.isArray(profile[key]) ? profile[key] : [profile[key]];
    group.querySelectorAll("input").forEach((input) => { input.checked = values.map(String).includes(input.value); });
  });
}

function renderRecruitmentJobs(jobs) {
  elements.recruitmentJobs.replaceChildren();
  if (!jobs.length) {
    elements.recruitmentJobs.innerHTML = '<div class="empty-list">暂时没有匹配岗位。完善画像后再试。</div>';
    return;
  }
  jobs.forEach((job) => {
    const card = document.createElement("article");
    card.className = "recruitment-job-card";
    const rate = job.estimated_rate == null ? "—" : `${job.estimated_rate}%`;
    const deadline = job.days_left == null ? "截止日期待确认" : job.days_left < 0 ? "已过截止日期" : `${job.days_left} 天后截止`;
    card.innerHTML = `<div class="job-card-top"><div><span class="job-company">${DOMPurify.sanitize(job.company)}</span><span class="job-type">${DOMPurify.sanitize(job.employer_type)}</span></div><div class="job-rank"><span class="job-tier ${DOMPurify.sanitize(job.tier_code || "T3")}">${DOMPurify.sanitize(job.tier_code || "T3")} ${DOMPurify.sanitize(job.tier_label || "保底")}</span><span class="job-score">${job.match_score}% 匹配</span></div></div><h4>${DOMPurify.sanitize(job.title)}</h4><p class="job-meta">${DOMPurify.sanitize(job.city)} · ${DOMPurify.sanitize(job.industry)} · ${deadline}</p><p class="job-requirements">${DOMPurify.sanitize(job.requirements)}</p><div class="job-card-bottom"><span>历史录取率 ${job.historical_rate == null ? "暂无" : `${job.historical_rate}%`}</span><strong>你的估计录取率 ${rate}</strong><a href="${DOMPurify.sanitize(job.url || "#")}" target="_blank" rel="noreferrer">查看来源 ↗</a></div>`;
    elements.recruitmentJobs.appendChild(card);
  });
}

async function refreshRecruitment() {
  try {
    const [profile, data] = await Promise.all([api("/recruitment/profile"), api("/recruitment/jobs")]);
    state.recruitmentProfile = profile;
    state.recruitmentJobs = data.jobs || [];
    renderRecruitmentProfile(profile);
    renderRecruitmentJobs(state.recruitmentJobs);
    elements.recruitmentStatus.textContent = data.data_status?.mode === "sample" ? "演示数据 · 待接入实时源" : "实时同步";
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    elements.recruitmentStatus.textContent = "加载失败";
  }
}

async function refreshRecruitmentSource() {
  elements.recruitmentRefresh.disabled = true;
  try {
    await api("/recruitment/refresh", { method: "POST" });
    await refreshRecruitment();
    showToast("岗位源已刷新。", 3500);
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    showToast("当前还没有配置官方实时岗位源。", 4500);
  } finally { elements.recruitmentRefresh.disabled = false; }
}

async function openRecruitment() {
  elements.recruitmentError.textContent = "";
  elements.recruitmentDialog.showModal();
  await refreshRecruitment();
}

async function saveRecruitment(event) {
  event.preventDefault();
  const employerTypes = [...document.querySelectorAll(".recruitment-checks input:checked")].map((input) => input.value);
  const choice = (key) => document.querySelector(`[data-choice-group="${key}"] input:checked`)?.value || "";
  const choices = (key) => [...document.querySelectorAll(`[data-choice-group="${key}"] input:checked`)].map((input) => input.value);
  try {
    const profile = await api("/recruitment/profile", {
      method: "PUT",
      body: JSON.stringify({
        desired_roles: splitRecruitmentValues(elements.recruitmentRoles.value),
        industries: splitRecruitmentValues(elements.recruitmentIndustries.value),
        locations: splitRecruitmentValues(elements.recruitmentLocations.value),
        employer_types: employerTypes,
        background: elements.recruitmentBackground.value.trim(),
        education_level: choice("education_level"),
        major_category: choice("major_category"),
        school_tier: choice("school_tier"),
        experience_level: choice("experience_level"),
        skill_tags: choices("skill_tags"),
        language_level: choice("language_level"),
        undergraduate_major: choice("undergraduate_major"),
        undergraduate_school_tier: choice("undergraduate_school_tier"),
        master_major: choice("master_major"),
        master_school_tier: choice("master_school_tier"),
        composite_interest: choices("composite_interest").includes("true"),
        graduation_year: null,
        availability_start: elements.recruitmentStart.value || null,
        availability_end: elements.recruitmentEnd.value || null,
      }),
    });
    state.recruitmentProfile = profile;
    const data = await api("/recruitment/jobs");
    state.recruitmentJobs = data.jobs || [];
    renderRecruitmentJobs(state.recruitmentJobs);
    elements.recruitmentStatus.textContent = data.data_status?.mode === "sample" ? "演示数据 · 待接入实时源" : "实时同步";
    showToast("求职画像已保存，岗位匹配已更新。", 3500);
  } catch (error) { elements.recruitmentError.textContent = translateError(error.message); }
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
$("studio-open").addEventListener("click", openStudio);
$("recruitment-open").addEventListener("click", openRecruitment);
$("recruitment-close").addEventListener("click", () => elements.recruitmentDialog.close());
elements.recruitmentRefresh.addEventListener("click", refreshRecruitmentSource);
elements.recruitmentForm.addEventListener("submit", saveRecruitment);
$("studio-open-secondary").addEventListener("click", openStudio);
$("mobile-studio-open").addEventListener("click", openStudio);
$("studio-close").addEventListener("click", () => elements.studioDialog.close());
elements.spaceForm.addEventListener("submit", createSpace);
elements.runnerSend.addEventListener("click", runActiveSpace);
$("billing-upgrade").addEventListener("click", explainBillingSetup);
$("cross-exam-open").addEventListener("click", openCrossExam);
$("cross-exam-close").addEventListener("click", () => elements.crossExamDialog.close());
elements.crossExamRun.addEventListener("click", runCrossExam);
document.querySelectorAll("[data-cross-focus]").forEach((button) => {
  button.addEventListener("click", () => {
    elements.crossExamFocus.value = button.dataset.crossFocus;
    elements.crossExamFocus.focus();
  });
});
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
