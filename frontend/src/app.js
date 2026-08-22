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
  legal: { symbol: "§", eyebrow: "FROST", themeName: "寒冰域", label: "寒冰域", hero: "有些东西决定世界如何运行，也决定什么不能被越过。", description: "当前从合同、合规、义务、期限与风险开始。", lens: "来源" },
  general: { symbol: "✦", eyebrow: "AURORA", themeName: "极光域", label: "极光域", hero: "让散落的信息逐渐形成属于你的知识世界。", description: "当前从资料、文档、对话与可追溯问答开始。", lens: "来源" },
  finance: { symbol: "↗", eyebrow: "EMBER", themeName: "烈火域", label: "烈火域", hero: "世界不只需要被理解，还需要决定向哪里前进。", description: "当前从数字、金融、风险、假设与决策分析开始。", lens: "来源" },
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
  spacePreflightTimer: null,
  recruitmentProfile: null,
  recruitmentJobs: [],
  recruitmentWatches: [],
  pendingLaunch: null,
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
  runnerRoute: $("runner-route"), runnerEstimatedInput: $("runner-estimated-input"),
  runnerOutputCeiling: $("runner-output-ceiling"), runnerCallCount: $("runner-call-count"),
  runnerCacheState: $("runner-cache-state"), runnerImpact: $("runner-impact"),
  runnerHistory: $("runner-history"), runnerHistoryRefresh: $("runner-history-refresh"),
  recruitmentDialog: $("recruitment-dialog"), recruitmentForm: $("recruitment-form"),
  recruitmentRoles: $("recruitment-roles"), recruitmentIndustries: $("recruitment-industries"), recruitmentLocations: $("recruitment-locations"),
  recruitmentJobs: $("recruitment-jobs"), recruitmentStatus: $("recruitment-source-status"),
  recruitmentRefresh: $("recruitment-refresh"), recruitmentSave: $("recruitment-save"),
  recruitmentError: $("recruitment-error"), recruitmentMonitorPools: $("recruitment-monitor-pools"),
  recruitmentDeadlineAlerts: $("recruitment-deadline-alerts"),
  recruitmentWatchForm: $("recruitment-watch-form"), recruitmentWatchCompany: $("recruitment-watch-company"),
  recruitmentWatchAdd: $("recruitment-watch-add"), recruitmentWatchList: $("recruitment-watch-list"),
  homeDeadlineAlerts: $("home-deadline-alerts"), homeAlertTitle: $("home-alert-title"),
  homeAlertList: $("home-alert-list"),
  resonanceDialog: $("resonance-dialog"), traceDialog: $("trace-dialog"), productMoreDialog: $("product-more-dialog"),
};

function activeWorkspace() {
  const rawWorkspace = state.workspaces.find((item) => item.id === state.workspace) || {
    id: "general",
    label: "通用文档",
    description: "围绕个人资料进行可追溯的问答与总结。",
    boundary: "请核对关键事实与来源。",
    hero: "把散落的信息，凝成可验证的洞见。",
    lens: "来源覆盖",
    quick_actions: [],
  };
  const meta = WORKSPACE_META[state.workspace] || WORKSPACE_META.general;
  return {
    ...rawWorkspace,
    label: meta.label,
    hero: meta.hero,
    description: meta.description,
    lens: meta.lens,
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
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), options.timeoutMs || 15000);
  try {
    const response = await fetch(API_BASE + path, { ...options, headers, signal: controller.signal });
    if (response.status === 401 && !path.startsWith("/auth/login")) await logout(false);
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      throw new Error(detail);
    }
    return response.status === 204 ? null : response.json();
  } catch (error) {
    if (error.name === "AbortError") throw new Error("请求超时，请检查服务是否已启动或稍后重试。");
    throw error;
  } finally {
    clearTimeout(timeout);
  }
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
    "OpenAI API could not complete the cross-examination.": "OpenAI API 暂时未能完成双域审查，请稍后重试。",
    "Cross-examination requires at least one document in both the legal and finance workspaces.": "请先在寒冰域和烈火域各上传至少一份资料。",
    "Your current plan has reached its AI Space limit.": "当前方案已达到可创造世界数量上限。",
    "The requested Space token budget exceeds your plan limit.": "这个世界的 Token 上限超过当前方案允许范围。",
    "This AI Space has reached its monthly Token budget.": "这个世界已达到本月 Token 上限。",
    "This run would exceed the AI Space monthly token budget.": "这次演化的预估消耗会超过世界的月度 Token 预算，模型尚未被调用。",
    "Only public HTTPS recruitment pages can be watched.": "只支持公开的 HTTPS 企业招聘页面。",
    "This address is not allowed for recruitment monitoring.": "这个地址不能加入监控，请使用企业公开招聘官网。",
    "Consent to the current privacy policy is required.": "请先同意当前版本的隐私政策。",
  };
  return known[message] || message;
}

async function enterApp() {
  elements.authView.classList.add("hidden");
  elements.appView.classList.remove("hidden");
  applyUser();
  await loadWorkspaces();
  await Promise.all([loadSessions(), loadDocuments(), loadHomeRecruitmentAlerts()]);
  newConversation();
  const pendingLaunch = state.pendingLaunch;
  state.pendingLaunch = null;
  if (pendingLaunch) window.setTimeout(() => launchProduct(pendingLaunch), 0);
  if (!state.user.privacy_accepted && !elements.consentDialog.open) elements.consentDialog.showModal();
}

async function loadHomeRecruitmentAlerts() {
  try {
    const [data, watchData] = await Promise.all([
      api("/recruitment/jobs"),
      api("/recruitment/watches").catch(() => ({ watches: [] })),
    ]);
    state.recruitmentJobs = data.jobs || [];
    state.recruitmentWatches = watchData.watches || watchData || [];
    renderHomeRecruitmentAlerts(state.recruitmentJobs, state.recruitmentWatches);
  } catch (_) {
    elements.homeDeadlineAlerts.classList.add("hidden");
  }
}

function watchHasFreshChange(watch) {
  if (Object.prototype.hasOwnProperty.call(watch, "change_pending")) return watch.change_pending === true;
  if (watch.changed === true || watch.change_detected === true || watch.has_changed === true) return true;
  const changedAt = watch.last_changed_at || watch.changed_at;
  if (!changedAt) return false;
  const timestamp = Date.parse(changedAt);
  return Number.isFinite(timestamp) && Date.now() - timestamp <= 7 * 24 * 60 * 60 * 1000;
}

function renderHomeRecruitmentAlerts(jobs, watches = state.recruitmentWatches) {
  const urgent = jobs
    .filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0 && job.days_left <= 7)
    .sort((a, b) => a.days_left - b.days_left);
  const changedWatches = (watches || []).filter(watchHasFreshChange);
  elements.homeAlertList.replaceChildren();
  if (!urgent.length && !changedWatches.length) {
    elements.homeDeadlineAlerts.classList.add("hidden");
    return;
  }
  const labels = [];
  if (urgent.length) labels.push(`${urgent.length} 个网申临近截止`);
  if (changedWatches.length) labels.push(`${changedWatches.length} 个官网有变化`);
  elements.homeAlertTitle.textContent = labels.join(" · ");
  urgent.forEach((job) => {
    const link = document.createElement("a");
    link.href = job.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    const urgency = job.days_left === 0 ? "今天截止" : `剩 ${job.days_left} 天`;
    link.append(
      makeElement("span", "", job.company || "招聘单位"),
      makeElement("strong", "", job.title || "校招岗位"),
      makeElement("b", "", urgency),
    );
    elements.homeAlertList.appendChild(link);
  });
  changedWatches.forEach((watch) => {
    const link = document.createElement("a");
    link.href = watch.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    link.append(
      makeElement("span", "", "官网变化"),
      makeElement("strong", "", watch.name || "招聘页面"),
      makeElement("b", "", "去核对 ↗"),
    );
    elements.homeAlertList.appendChild(link);
  });
  elements.homeDeadlineAlerts.classList.remove("hidden");
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
    label.textContent = WORKSPACE_META[workspaceId].label;
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
  elements.lensLabel.textContent = workspace.lens || "来源";
  elements.knowledgeTitle.textContent = `${workspace.label}资料库`;
  elements.uploadLabel.textContent = `投放到${meta.themeName}`;
  elements.workspaceBoundary.textContent = `${workspace.boundary} 文档片段和消息会由 OpenAI API 处理。`;
  elements.messageInput.placeholder = state.workspace === "legal"
    ? "询问条款、义务、期限、风险或合规证据…"
    : state.workspace === "finance"
      ? "询问指标变化、计算口径、假设或风险…"
      : "发送消息，或询问已上传的资料…";
  elements.composerHint.textContent = `${workspace.boundary} 回答中的“来源”表示来源覆盖，不代表结论必然正确。`;
  elements.evidenceTitle.textContent = workspace.lens || "来源";
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
    elements.crossExamError.textContent = "请先在寒冰域和烈火域各上传至少一份资料。";
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
    makeElement("h3", "", result.headline || "双域审查结果"),
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
    makeElement("h3", "", "本次双域审查未完成"),
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
  elements.billingPlan.textContent = `${plan} · ${billing.space_count}/${billing.limits?.max_spaces || 0} 个世界`;
  elements.billingUsage.textContent = `世界 ${used.toLocaleString()} / ${total.toLocaleString()} Tokens · ${billing.period}`;
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
      "还没有世界。选择一个世界原型，为它定义目标、规则与可演化的记录。",
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
    elements.runnerOutput.textContent = "描述这个世界要新增、改变或验证的内容。系统会先给出 Token 飞行计划，再决定走零 Token、缓存或模型路径。";
  }
  renderSpaces();
  loadSpaceHistory();
  scheduleSpacePreflight();
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

function openConcept(dialog) {
  closePanels();
  if (!dialog.open) dialog.showModal();
}

function closeConcept(dialogId) {
  const dialog = $(dialogId);
  if (dialog?.open) dialog.close();
}

async function launchProduct(product) {
  if (product === "resonance") return openConcept(elements.resonanceDialog);
  if (product === "trace") return openConcept(elements.traceDialog);
  if (!state.token) {
    state.pendingLaunch = product;
    if (WORKSPACE_ORDER.includes(product)) {
      state.workspace = product;
      await storage.set(STORAGE_KEYS.workspace, product);
    }
    document.querySelector(".auth-card")?.scrollIntoView({ behavior: "smooth", block: "center" });
    showToast("登录后即可进入这个世界。", 3200);
    return;
  }
  if (WORKSPACE_ORDER.includes(product)) {
    if (product !== state.workspace) await changeWorkspace(product);
    return;
  }
  if (product === "recruitment") return openRecruitment();
  if (product === "forge") return openStudio();
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
    showToast("世界已创造，现在可以预检第一项演化。", 5000);
    await haptic();
  } catch (error) {
    elements.spaceFormError.textContent = translateError(error.message);
  } finally {
    elements.spaceCreate.disabled = false;
    elements.spaceCreate.querySelector("span").textContent = "创造";
  }
}

function activeRunnerMode() {
  return document.querySelector('input[name="runner-mode"]:checked')?.value || "lean";
}

function updateRunnerModeCopy() {
  const mode = activeRunnerMode();
  const labels = { local: "零 Token 整理", lean: "执行节能演化", deep: "执行深度重算" };
  elements.runnerSend.querySelector("span").textContent = labels[mode] || labels.lean;
  scheduleSpacePreflight();
}

function resetSpacePreflight(message = "输入任务后自动预检") {
  elements.runnerRoute.textContent = message;
  elements.runnerEstimatedInput.textContent = "—";
  elements.runnerOutputCeiling.textContent = "—";
  elements.runnerCallCount.textContent = "—";
  elements.runnerCacheState.textContent = "—";
  elements.runnerImpact.textContent = "预检由应用服务器计算，不调用 OpenAI。";
}

async function previewActiveSpace() {
  const message = elements.runnerInput.value.trim();
  if (!state.activeSpaceId || !message) {
    resetSpacePreflight();
    return;
  }
  const mode = activeRunnerMode();
  elements.runnerRoute.textContent = "正在计算，不调用模型…";
  try {
    const preview = await api(`/spaces/${encodeURIComponent(state.activeSpaceId)}/preflight`, {
      method: "POST",
      body: JSON.stringify({ message, mode }),
    });
    const route = preview.execution_path || preview.path || preview.route || (mode === "local" ? "local" : "ai");
  const routeLabels = { local: "零 Token · 规则整理", cache: "缓存命中 · 直接复用", lean: "节能演化 · 单次模型调用", deep: "深度重算 · 单次模型调用", ai: "模型执行" };
    const inputEstimate = preview.estimated_input_tokens ?? preview.estimated_input ?? preview.input_tokens ?? 0;
    const outputCeiling = preview.max_output_tokens ?? preview.output_ceiling ?? 0;
    const cacheHit = Boolean(preview.cache_hit || route === "cache");
    const modelCalls = preview.model_calls ?? ((mode === "local" || cacheHit) ? 0 : 1);
    elements.runnerRoute.textContent = routeLabels[route] || route;
    elements.runnerEstimatedInput.textContent = `${Number(inputEstimate).toLocaleString()} T`;
    elements.runnerOutputCeiling.textContent = `${Number(outputCeiling).toLocaleString()} T`;
    elements.runnerCallCount.textContent = `${modelCalls} 次`;
    elements.runnerCacheState.textContent = cacheHit ? "已命中 · 0 T" : "未命中";
    elements.runnerImpact.textContent = preview.explanation || preview.impact || preview.message || "预算已在调用前核对；只有点击执行才可能调用模型。";
  } catch (error) {
    elements.runnerRoute.textContent = "预检失败";
    elements.runnerImpact.textContent = translateError(error.message);
  }
}

function scheduleSpacePreflight() {
  window.clearTimeout(state.spacePreflightTimer);
  state.spacePreflightTimer = window.setTimeout(previewActiveSpace, 260);
}

function renderSpaceHistory(runs = []) {
  elements.runnerHistory.replaceChildren();
  if (!runs.length) {
    elements.runnerHistory.appendChild(makeElement("small", "", "还没有成果记录；第一次执行后会在这里保留路径与用量。"));
    return;
  }
  runs.slice(0, 8).forEach((run) => {
    const item = document.createElement("article");
    item.className = "space-history-item";
    const path = run.execution_path || run.path || run.mode || "run";
    const message = run.message || run.input || run.task || "成果更新";
    const createdAt = run.created_at ? new Date(run.created_at).toLocaleString("zh-CN", { hour12: false }) : "";
    const total = run.total_tokens ?? run.usage?.total_tokens ?? 0;
    item.append(
      makeElement("span", "", path),
      (() => { const copy = document.createElement("div"); copy.append(makeElement("strong", "", message), makeElement("small", "", createdAt)); return copy; })(),
      makeElement("b", "", `${Number(total).toLocaleString()} T`),
    );
    elements.runnerHistory.appendChild(item);
  });
}

async function loadSpaceHistory() {
  if (!state.activeSpaceId) return;
  try {
    const data = await api(`/spaces/${encodeURIComponent(state.activeSpaceId)}/runs`);
    renderSpaceHistory(data.runs || data || []);
  } catch (_) {
      renderSpaceHistory([]);
  }
}

async function runActiveSpace() {
  const message = elements.runnerInput.value.trim();
  if (!state.activeSpaceId || !message || state.studioRunning) return;
  const spaceId = state.activeSpaceId;
  state.studioRunning = true;
  const mode = activeRunnerMode();
  elements.runnerSend.disabled = true;
  elements.runnerSend.querySelector("span").textContent = mode === "local" ? "正在规则整理…" : "正在执行…";
  elements.runnerOutput.textContent = mode === "local" ? "正在零 Token 整理这个世界…" : "正在按飞行计划演化；已先通过保守预算门槛…";
  try {
    const result = await api(`/spaces/${encodeURIComponent(spaceId)}/run`, {
      method: "POST",
      timeoutMs: 90000,
      body: JSON.stringify({ message, mode }),
    });
    const resultCopy = result.reply || result.output || (result.artifact ? `\`\`\`json\n${JSON.stringify(result.artifact, null, 2)}\n\`\`\`` : "没有返回成果内容。");
    elements.runnerOutput.innerHTML = safeMarkdown(resultCopy);
    const usage = result.usage || {};
    const path = result.execution_path || result.path || (result.cache_hit ? "cache" : mode);
    const footer = makeElement("div", "runner-usage-footer");
    const total = usage.total_tokens || 0;
    footer.append(
      makeElement("span", total === 0 ? "zero-token" : "", `本次实际 ${total.toLocaleString()} Tokens`),
      makeElement("span", "", `路径 ${path}`),
      makeElement("span", "", `输入 ${usage.input_tokens || 0} · 输出 ${usage.output_tokens || 0}`),
    );
    const saved = result.avoided_tokens ?? result.saved_tokens ?? 0;
    if (result.cache_hit && saved > 0) {
      footer.appendChild(makeElement("span", "zero-token", `复用成果，避免实际 ${Number(saved).toLocaleString()} Tokens`));
    } else if (mode === "local" && (result.estimated_tokens_saved || 0) > 0) {
      footer.appendChild(makeElement("span", "zero-token", `相对节能上限估算最多 ${Number(result.estimated_tokens_saved).toLocaleString()} Tokens`));
    }
    elements.runnerOutput.appendChild(footer);
    elements.runnerInput.value = "";
    if (result.billing) state.billing = result.billing;
    await refreshStudio();
    selectSpace(spaceId, true);
    await loadSpaceHistory();
    resetSpacePreflight("成果已更新，输入下一项变化");
    await haptic();
  } catch (error) {
    elements.runnerOutput.textContent = `运行失败：${translateError(error.message)}`;
  } finally {
    state.studioRunning = false;
    elements.runnerSend.disabled = false;
    updateRunnerModeCopy();
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

function setRecruitmentStatus(message) {
  if (elements.recruitmentStatus) elements.recruitmentStatus.textContent = message;
}

function renderRecruitmentProfile(profile) {
  if (!profile) return;
  elements.recruitmentRoles.value = (profile.desired_roles || []).join("，");
  elements.recruitmentIndustries.value = (profile.industries || []).join("，");
  elements.recruitmentLocations.value = (profile.locations || []).join("，");
  document.querySelectorAll(".recruitment-checks input").forEach((input) => {
    input.checked = (profile.employer_types || []).includes(input.value);
  });
}

function recruitmentWatchStatus(watch) {
  if (watch.last_status === "error" || watch.error || watch.last_error) return "暂时无法读取";
  if (watchHasFreshChange(watch)) return "官网内容有变化 · 待核对";
  if (watch.last_status === "baseline") return "基线已建立";
  if (watch.last_checked_at) return "已核对 · 暂无变化";
  return "等待首次建立基线";
}

function renderRecruitmentWatches(watches = []) {
  elements.recruitmentWatchList.replaceChildren();
  if (!watches.length) {
    elements.recruitmentWatchList.appendChild(makeElement("small", "", "尚未添加企业监控。填写企业名称即可跟踪岗位池变化。"));
    return;
  }
  watches.forEach((watch) => {
    const card = document.createElement("article");
    card.className = `watch-card${watchHasFreshChange(watch) ? " changed" : ""}`;
    const top = makeElement("div", "watch-card-top");
    const copy = document.createElement("div");
    if (watch.watch_type === "company") {
      copy.append(
        makeElement("strong", "", watch.company_name || watch.name || "企业岗位池"),
        makeElement("small", "watch-target-copy", "校招岗位池动态监控"),
      );
    } else {
      const link = makeElement("a", "", watch.url || "");
      link.href = watch.url;
      link.target = "_blank";
      link.rel = "noreferrer";
      copy.append(makeElement("strong", "", watch.name || "招聘官网"), link);
    }
    const actions = makeElement("div", "watch-card-actions");
    if (watchHasFreshChange(watch)) {
      const acknowledge = makeElement("button", "watch-acknowledge", "已核对");
      acknowledge.type = "button";
      acknowledge.addEventListener("click", () => acknowledgeRecruitmentWatch(watch.id, watch.change_version || 0));
      actions.appendChild(acknowledge);
    }
    const remove = makeElement("button", "watch-delete", "×");
    remove.type = "button";
    remove.title = "停止监控";
    remove.addEventListener("click", () => deleteRecruitmentWatch(watch.id));
    actions.appendChild(remove);
    top.append(copy, actions);
    const statuses = makeElement("div", "watch-card-status");
    statuses.appendChild(makeElement("span", "", recruitmentWatchStatus(watch)));
    const checkedAt = watch.last_checked_at ? new Date(watch.last_checked_at).toLocaleString("zh-CN", { hour12: false }) : "尚未检查";
    statuses.appendChild(makeElement("span", "", checkedAt));
    const keywordList = Array.isArray(watch.keywords) ? watch.keywords : [];
    const excerpt = watch.excerpt || watch.change_excerpt || (
      watch.watch_type === "company"
        ? (keywordList.length ? `已在岗位池中跟踪：${keywordList.join(" · ")}` : "新岗位进入池子后自动提示。")
        : (keywordList.length ? `关注：${keywordList.join(" · ")}` : "系统只比较公开网页文本指纹，不调用模型。")
    );
    card.append(top, statuses, makeElement("p", "", excerpt));
    elements.recruitmentWatchList.appendChild(card);
  });
}

async function addRecruitmentWatch(event) {
  event.preventDefault();
  if (!elements.recruitmentWatchForm.reportValidity()) return;
  elements.recruitmentWatchAdd.disabled = true;
  elements.recruitmentWatchAdd.querySelector("span").textContent = "正在建立基线…";
  elements.recruitmentError.textContent = "";
  try {
    const createdWatch = await api("/recruitment/watches", {
      method: "POST",
      timeoutMs: 12000,
      body: JSON.stringify({
        company_name: elements.recruitmentWatchCompany.value.trim(),
      }),
    });
    elements.recruitmentWatchForm.reset();
    await refreshRecruitment();
    showToast(
      createdWatch.last_status === "error"
        ? "已加入动态雷达，但首次基线尚未建立；请稍后刷新重试。"
        : "已加入动态雷达；首次内容作为基线，不会误报变化。",
      5000,
    );
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
  } finally {
    elements.recruitmentWatchAdd.disabled = false;
    elements.recruitmentWatchAdd.querySelector("span").textContent = "加入动态雷达";
  }
}

async function deleteRecruitmentWatch(watchId) {
  try {
    await api(`/recruitment/watches/${encodeURIComponent(watchId)}`, { method: "DELETE" });
    await refreshRecruitment();
    showToast("已停止监控这个页面。", 3000);
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
  }
}

async function acknowledgeRecruitmentWatch(watchId, changeVersion) {
  try {
    await api(`/recruitment/watches/${encodeURIComponent(watchId)}/acknowledge`, {
      method: "POST",
      body: JSON.stringify({ change_version: changeVersion }),
    });
    await refreshRecruitment();
    showToast("已标记为核对完成；下次页面变化会再次提醒。", 3500);
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
  }
}

function renderRecruitmentMonitors(pools = []) {
  elements.recruitmentMonitorPools.replaceChildren();
  pools.forEach((pool) => {
    const card = document.createElement("article");
    card.className = "recruitment-monitor-card";
    const employers = (pool.employers || []).join(" · ");
    card.innerHTML = `<div><strong>${DOMPurify.sanitize(pool.name)}</strong><span>${pool.employers?.length || 0} 个重点机构</span></div><p>${DOMPurify.sanitize(pool.focus || "")}</p><details><summary>查看全部监控机构</summary><small>${DOMPurify.sanitize(employers)}</small></details>`;
    elements.recruitmentMonitorPools.appendChild(card);
  });
}

function renderRecruitmentDeadlineAlerts(jobs) {
  elements.recruitmentDeadlineAlerts.replaceChildren();
  const urgent = jobs.filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0 && job.days_left <= 7);
  const dated = jobs.filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0);
  const heading = document.createElement("strong");
  heading.textContent = urgent.length
    ? `网申截止预警 · ${urgent.length} 个校招岗位将在 7 天内截止`
    : dated.length
      ? "网申截止预警 · 暂无 7 天内到期的已核验校招岗位"
      : "网申截止预警 · 暂无公告明确标注截止日期，刷新后将自动核验";
  const list = document.createElement("div");
  urgent.forEach((job) => {
    const item = document.createElement("a");
    item.href = job.url || "#";
    item.target = "_blank";
    item.rel = "noreferrer";
    item.textContent = `${job.company}｜${job.title}｜${job.days_left === 0 ? "今天截止" : `${job.days_left} 天后截止`}`;
    list.appendChild(item);
  });
  elements.recruitmentDeadlineAlerts.append(heading, list);
}

function renderRecruitmentJobs(jobs) {
  elements.recruitmentJobs.replaceChildren();
  if (!jobs.length) {
    elements.recruitmentJobs.innerHTML = '<div class="empty-list">暂时没有匹配岗位。调整筛选或刷新岗位源后再试。</div>';
    return;
  }
  const availableJobs = jobs.filter((job) => /^https:\/\//.test(job.url || ""));
  const tierDefinitions = [
    ["T0", "强匹配", "90–98"], ["T0.5", "高匹配", "85–89"],
    ["T1", "主力", "78–84"], ["T1.5", "较主力", "72–77"],
    ["T2", "可投", "64–71"], ["T2.5", "观察", "56–63"], ["T3", "低匹配", "0–55"],
  ];
  const tierOrder = tierDefinitions.map(([code]) => code);
  const tierSummary = makeElement("div", "job-tier-summary");
  tierDefinitions.forEach(([tier, label, range]) => {
    const count = availableJobs.filter((job) => job.tier_code === tier).length;
    const tierClass = tier.replace(".", "-");
    const badge = makeElement("span", `job-tier-summary-item ${tierClass}`);
    badge.title = `${label}：匹配分 ${range}`;
    badge.append(makeElement("b", "", tier), makeElement("small", "", `${label} · ${count}`));
    tierSummary.appendChild(badge);
  });
  elements.recruitmentJobs.appendChild(tierSummary);
  elements.recruitmentJobs.appendChild(
    makeElement("p", "job-tier-legend", "分层规则：T0 强匹配 ≥90 · T0.5 高匹配 85–89 · T1 主力 78–84 · T1.5 较主力 72–77 · T2 可投 64–71 · T2.5 观察 56–63 · T3 低匹配 ≤55；这是匹配优先级，不是录取概率。"),
  );
  tierOrder.forEach((tier) => {
    const tierJobs = availableJobs.filter((job) => (job.tier_code || "T3") === tier);
    if (!tierJobs.length) return;
    const group = makeElement("section", "recruitment-tier-group");
    const heading = makeElement("div", "recruitment-tier-heading");
    heading.append(makeElement("strong", `job-tier ${tier.replace(".", "-")}`, tier), makeElement("span", "", `${tierJobs.length} 个匹配岗位`));
    group.appendChild(heading);
    tierJobs.forEach((job) => {
    const card = document.createElement("article");
    card.className = "recruitment-job-card";
    const deadline = job.days_left == null ? "截止日期待官方确认" : (job.days_left === 0 ? "今天截止" : `${job.days_left} 天后截止`);
    const tierCode = tierOrder.includes(job.tier_code) ? job.tier_code : "T3";
    const top = makeElement("div", "job-card-top");
    const labels = document.createElement("div");
    labels.append(
      makeElement("span", "job-company", job.company || "招聘单位"),
      makeElement("span", "job-type", job.employer_type || "重点雇主"),
    );
    const rank = makeElement("div", "job-rank");
    rank.appendChild(makeElement("span", `job-tier ${tierCode.replace(".", "-")}`, tierCode));
    top.append(labels, rank);
    const bottom = makeElement("div", "job-card-bottom");
    const officialLink = makeElement("a", "", "打开校招公告 ↗");
    officialLink.href = job.url;
    officialLink.target = "_blank";
    officialLink.rel = "noreferrer";
    bottom.appendChild(officialLink);
    card.append(
      top,
      makeElement("h4", "", job.title || "校招岗位"),
      makeElement("p", "job-meta", `${job.city || "地点待确认"} · ${job.industry || "行业待确认"} · ${deadline}`),
      makeElement("p", "job-requirements", job.requirements || "请打开官方公告核对申请条件。"),
      bottom,
    );
    const watchButton = makeElement("button", "job-watch-button", "跟踪此公告变化");
    watchButton.type = "button";
    watchButton.addEventListener("click", () => addRecruitmentWatchFromJob(job, watchButton));
    bottom.appendChild(watchButton);
      group.appendChild(card);
    });
    elements.recruitmentJobs.appendChild(group);
  });
}

async function addRecruitmentWatchFromJob(job, button) {
  button.disabled = true;
  button.textContent = "正在建立基线…";
  elements.recruitmentError.textContent = "";
  try {
    const createdWatch = await api("/recruitment/watches", {
      method: "POST",
      timeoutMs: 12000,
      body: JSON.stringify({
        name: `${job.company} · ${job.title}`.slice(0, 80),
        url: job.url,
        keywords: [job.company, job.title].filter(Boolean).slice(0, 12),
      }),
    });
    await refreshRecruitment();
    showToast(
      createdWatch.last_status === "error"
        ? "已加入动态雷达；官网暂时无法建立基线，可稍后重试。"
        : "已开始跟踪此校招公告；页面变化会在首页待核对区提示。",
      5000,
    );
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    button.disabled = false;
    button.textContent = "跟踪此公告变化";
  }
}

async function refreshRecruitment() {
  try {
    const [profile, data, watchData] = await Promise.all([
      api("/recruitment/profile"),
      api("/recruitment/jobs"),
      api("/recruitment/watches").catch(() => ({ watches: [] })),
    ]);
    state.recruitmentProfile = profile;
    state.recruitmentJobs = data.jobs || [];
    state.recruitmentWatches = watchData.watches || watchData || [];
    renderRecruitmentProfile(profile);
    renderRecruitmentJobs(state.recruitmentJobs);
    renderRecruitmentWatches(state.recruitmentWatches);
    renderRecruitmentDeadlineAlerts(state.recruitmentJobs);
    renderHomeRecruitmentAlerts(state.recruitmentJobs, state.recruitmentWatches);
    renderRecruitmentMonitors(data.monitor_pools || []);
    setRecruitmentStatus(data.data_status?.message || "已读取动态岗位源");
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    setRecruitmentStatus("岗位源加载失败");
  }
}

async function refreshRecruitmentSource() {
  elements.recruitmentRefresh.disabled = true;
  try {
    const results = await Promise.allSettled([
      api("/recruitment/refresh", { method: "POST" }),
      api("/recruitment/watches/refresh", { method: "POST", timeoutMs: 35000 }),
    ]);
    if (results.every((result) => result.status === "rejected")) throw results[0].reason;
    await refreshRecruitment();
    const jobsOk = results[0].status === "fulfilled";
    const watchesOk = results[1].status === "fulfilled";
    const sourceResult = jobsOk ? results[0].value : null;
    const sourceCopy = sourceResult
      ? sourceResult.cached
        ? "沿用 60 秒内的公开源结果"
        : `读取 ${Number(sourceResult.count || 0).toLocaleString()} 条公开源候选`
      : "公开岗位源本次未完成";
    const webSearch = sourceResult?.web_search;
    const webSearchCopy = webSearch?.status === "success"
      ? `AI 网页搜索发现 ${Number(webSearch.jobs || 0)} 条候选`
      : webSearch?.status === "error"
        ? "AI 网页搜索本轮未完成"
        : "AI 网页搜索按低频周期运行";
    showToast(
      jobsOk && watchesOk
        ? `${sourceCopy}；${webSearchCopy}；官网变化雷达已刷新。`
        : jobsOk
          ? `${sourceCopy}；${webSearchCopy}；官网变化雷达本次未完成。`
          : "官网变化雷达已刷新；公开岗位源本次未完成。",
      4500,
    );
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    showToast("刷新未完成，请稍后重试。", 4500);
  } finally { elements.recruitmentRefresh.disabled = false; }
}

async function openRecruitment() {
  elements.recruitmentError.textContent = "";
  elements.recruitmentDialog.showModal();
  await refreshRecruitment();
}

async function saveRecruitment(event) {
  event.preventDefault();
  const saveButton = elements.recruitmentSave;
  const saveLabel = saveButton?.querySelector("span");
  if (saveButton) saveButton.disabled = true;
  if (saveLabel) saveLabel.textContent = "匹配中…";
  elements.recruitmentError.textContent = "";
  setRecruitmentStatus("正在保存筛选并匹配岗位…");
  const employerTypes = [...document.querySelectorAll(".recruitment-checks input:checked")].map((input) => input.value);
  try {
    const profile = await api("/recruitment/profile", {
      method: "PUT",
      body: JSON.stringify({
        desired_roles: splitRecruitmentValues(elements.recruitmentRoles.value),
        industries: splitRecruitmentValues(elements.recruitmentIndustries.value),
        locations: splitRecruitmentValues(elements.recruitmentLocations.value),
        employer_types: employerTypes,
      }),
    });
    state.recruitmentProfile = profile;
    const data = await Promise.race([
      api("/recruitment/jobs"),
      new Promise((_, reject) => setTimeout(() => reject(new Error("岗位匹配请求超时，请稍后重试。")), 12000)),
    ]);
    state.recruitmentJobs = data.jobs || [];
    renderRecruitmentJobs(state.recruitmentJobs);
    renderRecruitmentDeadlineAlerts(state.recruitmentJobs);
    renderHomeRecruitmentAlerts(state.recruitmentJobs, state.recruitmentWatches);
    renderRecruitmentMonitors(data.monitor_pools || []);
    setRecruitmentStatus(data.data_status?.message || "已读取动态岗位源");
    showToast("筛选已保存，岗位匹配已更新。", 3500);
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    setRecruitmentStatus("保存未完成，可稍后重试");
  } finally {
    if (saveButton) saveButton.disabled = false;
    if (saveLabel) saveLabel.textContent = "保存筛选并重新匹配";
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
$("studio-open").addEventListener("click", openStudio);
$("recruitment-open").addEventListener("click", openRecruitment);
$("resonance-open").addEventListener("click", () => openConcept(elements.resonanceDialog));
$("trace-open").addEventListener("click", () => openConcept(elements.traceDialog));
$("mobile-recruitment-open").addEventListener("click", openRecruitment);
$("home-alert-open").addEventListener("click", openRecruitment);
$("recruitment-close").addEventListener("click", () => elements.recruitmentDialog.close());
elements.recruitmentRefresh.addEventListener("click", refreshRecruitmentSource);
elements.recruitmentForm.addEventListener("submit", saveRecruitment);
elements.recruitmentWatchForm.addEventListener("submit", addRecruitmentWatch);
$("studio-open-secondary").addEventListener("click", openStudio);
$("mobile-studio-open").addEventListener("click", openStudio);
$("mobile-more-open").addEventListener("click", () => openConcept(elements.productMoreDialog));
$("studio-close").addEventListener("click", () => elements.studioDialog.close());
elements.spaceForm.addEventListener("submit", createSpace);
elements.runnerSend.addEventListener("click", runActiveSpace);
elements.runnerInput.addEventListener("input", scheduleSpacePreflight);
document.querySelectorAll('input[name="runner-mode"]').forEach((input) => input.addEventListener("change", updateRunnerModeCopy));
elements.runnerHistoryRefresh.addEventListener("click", loadSpaceHistory);
$("billing-upgrade").addEventListener("click", explainBillingSetup);
$("cross-exam-open").addEventListener("click", openCrossExam);
$("more-cross-exam-open").addEventListener("click", () => {
  closeConcept("product-more-dialog");
  openCrossExam();
});
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
document.querySelectorAll("[data-launch]").forEach((button) => {
  button.addEventListener("click", () => launchProduct(button.dataset.launch));
});
document.querySelectorAll("[data-concept-open]").forEach((button) => {
  button.addEventListener("click", () => {
    closeConcept("product-more-dialog");
    openConcept($(button.dataset.conceptOpen));
  });
});
document.querySelectorAll("[data-close-concept]").forEach((button) => {
  button.addEventListener("click", () => closeConcept(button.dataset.closeConcept));
});
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
