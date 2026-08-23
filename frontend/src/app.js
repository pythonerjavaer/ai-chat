import DOMPurify from "dompurify";
import { marked } from "marked";
import { Capacitor } from "@capacitor/core";
import { Haptics, ImpactStyle } from "@capacitor/haptics";
import { Preferences } from "@capacitor/preferences";
import { MUSIC_CREATION_TEMPLATES, buildMusicBlueprint, soundscapeEngine } from "./music-creator.js";
import { initOblivionArchive, openOblivionArchive } from "./oblivion-archive.js";
import "./styles.css";

marked.setOptions({ gfm: true, breaks: true });

const configuredApiBase = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");
const API_BASE = configuredApiBase || "/api";
const STORAGE_KEYS = {
  token: "frostfire_token",
  workspace: "frostfire_workspace",
  musicEnabled: "bingyan_music_enabled",
  musicVolume: "bingyan_music_volume",
  musicBlueprint: "bingyan_music_blueprint",
  activeProduct: "bingyan_active_product",
  photonCreations: "bingyan_photon_creations",
};
const WORKSPACE_ORDER = ["legal", "general", "finance"];
const CHATGPT_MONITOR_SOURCE_COUNT = 5;
const RECRUITMENT_REFRESH_LABEL = "公开源 + AI 补漏 ↻";
const WORKSPACE_META = {
  legal: { symbol: "§", eyebrow: "FROST", themeName: "寒冰域", label: "寒冰域", hero: "有些东西决定世界如何运行，也决定什么不能被越过。", description: "当前从合同、合规、义务、期限与风险开始。", lens: "来源" },
  general: { symbol: "✦", eyebrow: "AURORA", themeName: "极光域", label: "极光域", hero: "让散落的信息逐渐形成属于你的知识世界。", description: "当前从资料、文档、对话与可追溯问答开始。", lens: "来源" },
  finance: { symbol: "↗", eyebrow: "EMBER", themeName: "烈火域", label: "烈火域", hero: "世界不只需要被理解，还需要决定向哪里前进。", description: "当前从数字、金融、风险、假设与决策分析开始。", lens: "来源" },
};
const PHOTON_TRACKS = {
  text: { label: "文字", purpose: "文案、文章、诗歌、演讲与表达", format: ["作品标题", "核心表达", "完整文本", "一句备选方向"] },
  visual: { label: "视觉", purpose: "海报、封面、摄影、概念图与图像提示词", format: ["视觉标题", "核心意象", "构图", "光线", "材质", "色彩方向", "可复制的图像生成提示词"] },
  narrative: { label: "叙事", purpose: "故事、角色、短片、MV、广告与分镜", format: ["标题", "核心冲突", "人物或主体", "结构", "关键场景", "结尾方向"] },
  brand: { label: "品牌", purpose: "命名、Slogan、品牌人格、语言与视觉方向", format: ["品牌名称", "品牌核心", "Slogan", "语言风格", "视觉方向", "一个备选方案"] },
  interface: { label: "界面", purpose: "网页、App、产品首页、交互与微文案", format: ["页面目标", "信息结构", "主视觉", "关键模块", "交互方式", "核心文案", "给 Codex 的简短实现说明"] },
  sound: { label: "声音概念", purpose: "曲风、结构、情绪、场景、专辑或 MV 方向", format: ["作品概念", "情绪曲线", "节奏与结构", "乐器或声音材质", "视觉联想", "MV 或现场方向"] },
};
const PHOTON_STYLE_META = {
  heat: ["冷静", "炽烈"], chaos: ["秩序", "混沌"], complexity: ["极简", "繁复"],
  surreal: ["现实", "超现实"], bold: ["克制", "狂放"], dark: ["明亮", "暗黑"],
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
  recruitmentTierFilter: "ALL",
  recruitmentWatches: [],
  recruitmentSyncStatus: null,
  pendingLaunch: null,
  activeProduct: null,
  music: {
    enabled: false,
    playing: false,
    loading: false,
    volume: 0.18,
    currentTrack: null,
    status: "idle",
    error: null,
    muted: false,
    minimized: false,
    source: "local_composer",
    creationTemplate: "cosmos",
    creationScale: "minor_pentatonic",
    creationInstruments: ["piano", "strings", "bass", "bells"],
    vocalProfile: "none",
  },
  photon: {
    loading: false,
    current: null,
    creations: [],
    creationsLoaded: false,
  },
};

const $ = (id) => document.getElementById(id);
const elements = {
  authView: $("auth-view"), appView: $("app-view"), authForm: $("auth-form"),
  authKicker: $("auth-kicker"), authTitle: $("auth-title"), authDescription: $("auth-description"),
  authSubmit: $("auth-submit"), authError: $("auth-error"), authSwitch: $("auth-switch"),
  authSwitchCopy: $("auth-switch-copy"), privacyRow: $("privacy-row"),
  authModeLogin: $("auth-mode-login"), authModeRegister: $("auth-mode-register"),
  privacyAccepted: $("privacy-accepted"), username: $("username"), password: $("password"),
  workspaceTabs: $("workspace-tabs"), workspacePanelTitle: $("workspace-panel-title"),
  workspaceEyebrow: $("workspace-eyebrow"), workspaceTitle: $("workspace-title"),
  workspaceHeroCopy: $("workspace-hero-copy"), workspaceIndicator: $("workspace-indicator"),
  sceneArrival: $("scene-arrival"), sceneArrivalEyebrow: $("scene-arrival-eyebrow"),
  sceneArrivalTitle: $("scene-arrival-title"), sceneArrivalCopy: $("scene-arrival-copy"),
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
  resonanceDialog: $("resonance-dialog"), traceDialog: $("trace-dialog"),
  musicDialog: $("music-dimension-dialog"), musicMiniPlayer: $("music-mini-player"),
  musicMiniTrack: $("music-mini-track"), musicMiniToggle: $("music-mini-toggle"),
  musicMiniMute: $("music-mini-mute"), musicDialogMinimize: $("music-dialog-minimize"),
  musicCurrentTrack: $("music-current-track"), musicCurrentArtist: $("music-current-artist"),
  musicStatus: $("music-status"), musicPlayToggle: $("music-play-toggle"),
  musicVolume: $("music-volume"), musicVolumeOutput: $("music-volume-output"),
  musicEnable: $("music-enable"), musicDisable: $("music-disable"), musicFooterMinimize: $("music-footer-minimize"),
  musicCreationTitle: $("music-creation-title"), musicCreationStyle: $("music-creation-style"),
  musicCreationMood: $("music-creation-mood"), musicCreationTempo: $("music-creation-tempo"),
  musicTempoOutput: $("music-tempo-output"), musicCreationTexture: $("music-creation-texture"),
  musicCreationDescription: $("music-creation-description"), musicBlueprint: $("music-blueprint"),
  musicRewrite: $("music-rewrite"), musicCopyBlueprint: $("music-copy-blueprint"), musicVocalProfile: $("music-vocal-profile"),
  photonDialog: $("photon-projection-dialog"), photonInspiration: $("photon-inspiration"),
  photonInputCount: $("photon-input-count"), photonWorldField: $("photon-world-field"),
  photonWorldSelect: $("photon-world-select"), photonWorldNote: $("photon-world-note"),
  photonSkeleton: $("photon-skeleton"), photonProject: $("photon-project"), photonError: $("photon-error"),
  photonResultMode: $("photon-result-mode"), photonResultTrack: $("photon-result-track"),
  photonResultSource: $("photon-result-source"), photonResultStyle: $("photon-result-style"),
  photonResultUsage: $("photon-result-usage"), photonResultBody: $("photon-result-body"),
  photonCopy: $("photon-copy"), photonSave: $("photon-save"), photonHistoryList: $("photon-history-list"),
  mobileWorldNavigation: $("mobile-world-navigation"),
  worldMapDialog: $("world-map-dialog"), worldMapCurrent: $("world-map-current"),
  adminUsageLauncher: $("admin-usage-launcher"), adminUsageDialog: $("admin-usage-dialog"),
  adminUsageAuth: $("admin-usage-auth"), adminUsageToken: $("admin-usage-token"),
  adminUsageConnect: $("admin-usage-connect"), adminUsageError: $("admin-usage-error"),
  adminUsageContent: $("admin-usage-content"), adminUsageStatus: $("admin-usage-status"),
  adminUsageUpdated: $("admin-usage-updated"), adminUsageCards: $("admin-usage-cards"),
  adminUsageSeries: $("admin-usage-series"), adminUsageRefresh: $("admin-usage-refresh"),
  adminUsageLock: $("admin-usage-lock"), adminUsageClose: $("admin-usage-close"),
};

if (elements.recruitmentRefresh) {
  elements.recruitmentRefresh.textContent = RECRUITMENT_REFRESH_LABEL;
  elements.recruitmentRefresh.title = "同步公开招聘来源，并在 15 分钟冷却允许时运行一次低频 AI 补漏；会产生少量 Token 消耗。";
  elements.recruitmentRefresh.setAttribute("aria-label", "刷新公开源并运行低频 AI 补漏");
}

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

const sceneEntryTimers = new WeakMap();

function playSceneEntry(target, duration = 2100) {
  if (!target) return;
  window.clearTimeout(sceneEntryTimers.get(target));
  target.classList.remove("scene-entering");
  void target.offsetWidth;
  target.classList.add("scene-entering");
  sceneEntryTimers.set(target, window.setTimeout(() => {
    target.classList.remove("scene-entering");
    sceneEntryTimers.delete(target);
  }, duration));
}

function playWorkspaceEntry(workspaceId) {
  const meta = WORKSPACE_META[workspaceId] || WORKSPACE_META.general;
  elements.appView.dataset.scene = workspaceId;
  elements.sceneArrivalEyebrow.textContent = meta.eyebrow;
  elements.sceneArrivalTitle.textContent = meta.themeName;
  elements.sceneArrivalCopy.textContent = meta.hero;
  playSceneEntry(elements.appView, 2400);
}

const rotaryCompasses = new Map();

function setupRotaryCompass(container) {
  if (!container || rotaryCompasses.has(container.dataset.rotaryCompass)) return;
  const id = container.dataset.rotaryCompass;
  const cards = [...container.querySelectorAll("[data-launch]")];
  if (!cards.length) return;
  const compass = {
    id,
    container,
    cards,
    rotation: 0,
    startRotation: 0,
    startX: 0,
    startY: 0,
    dragging: false,
    moved: false,
    suppressNextClick: false,
    selectedIndex: 0,
    wheelLocked: false,
  };
  const step = () => (Math.PI * 2) / compass.cards.length;
  const render = () => {
    const compact = window.innerWidth <= 520;
    const radiusX = compact ? Math.min(122, container.clientWidth * .36) : Math.min(id === "landing" ? 320 : 300, container.clientWidth * .39);
    const radiusY = compact ? 62 : id === "landing" ? 104 : 94;
    let selectedIndex = 0;
    let selectedDepth = -1;
    compass.cards.forEach((card, index) => {
      const angle = compass.rotation + index * step();
      const cosine = Math.cos(angle);
      const depth = (cosine + 1) / 2;
      const scale = .54 + depth * .46;
      const x = Math.sin(angle) * radiusX;
      const y = cosine * radiusY;
      card.style.setProperty("--compass-x", `${x}px`);
      card.style.setProperty("--compass-y", `${y}px`);
      card.style.setProperty("--compass-scale", scale.toFixed(3));
      card.style.setProperty("--compass-opacity", (.12 + depth * .88).toFixed(3));
      card.style.setProperty("--compass-blur", `${((1 - depth) * 2.6).toFixed(2)}px`);
      card.style.zIndex = String(Math.round(10 + depth * 90));
      card.dataset.compassDepth = depth.toFixed(3);
      if (depth > selectedDepth) {
        selectedDepth = depth;
        selectedIndex = index;
      }
    });
    compass.selectedIndex = selectedIndex;
    compass.cards.forEach((card, index) => {
      const visible = Number(card.dataset.compassDepth) >= .08;
      const selected = index === selectedIndex;
      card.setAttribute("aria-hidden", String(!visible));
      card.setAttribute("data-compass-selected", String(selected));
      card.setAttribute("aria-current", selected ? "true" : "false");
      card.tabIndex = selected ? 0 : -1;
    });
    container.dataset.selectedProduct = compass.cards[selectedIndex].dataset.launch || "";
  };
  const snap = () => {
    compass.rotation = Math.round(compass.rotation / step()) * step();
    container.classList.add("is-snapping");
    render();
    window.setTimeout(() => container.classList.remove("is-snapping"), 360);
  };
  const rotate = (direction) => {
    compass.rotation -= direction * step();
    snap();
  };
  container.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    compass.dragging = true;
    compass.moved = false;
    compass.startX = event.clientX;
    compass.startY = event.clientY;
    compass.startRotation = compass.rotation;
    container.classList.add("is-dragging");
    container.setPointerCapture(event.pointerId);
  });
  container.addEventListener("pointermove", (event) => {
    if (!compass.dragging) return;
    const deltaX = event.clientX - compass.startX;
    const deltaY = event.clientY - compass.startY;
    if (Math.hypot(deltaX, deltaY) > 5) compass.moved = true;
    if (compass.moved && event.cancelable && Math.abs(deltaX) >= Math.abs(deltaY)) event.preventDefault();
    compass.rotation = compass.startRotation + deltaX / Math.max(150, container.clientWidth) * Math.PI * 2;
    render();
  });
  const endDrag = (event) => {
    if (!compass.dragging) return;
    compass.dragging = false;
    container.classList.remove("is-dragging");
    if (compass.moved) {
      compass.suppressNextClick = true;
      window.setTimeout(() => { compass.suppressNextClick = false; }, 120);
    }
    if (container.hasPointerCapture(event.pointerId)) container.releasePointerCapture(event.pointerId);
    snap();
  };
  container.addEventListener("pointerup", endDrag);
  container.addEventListener("pointercancel", endDrag);
  container.addEventListener("click", (event) => {
    if (compass.suppressNextClick) {
      compass.suppressNextClick = false;
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    const card = event.target.closest?.('[data-launch][data-compass-selected="true"]');
    if (!card || !container.contains(card)) return;
    snap();
  }, true);
  container.addEventListener("wheel", (event) => {
    if (compass.wheelLocked) return;
    const amount = Math.abs(event.deltaX) > Math.abs(event.deltaY) ? event.deltaX : event.deltaY;
    if (Math.abs(amount) < 4) return;
    event.preventDefault();
    compass.wheelLocked = true;
    rotate(amount > 0 ? 1 : -1);
    window.setTimeout(() => { compass.wheelLocked = false; }, 180);
  }, { passive: false });
  container.addEventListener("keydown", (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault();
    rotate(event.key === "ArrowRight" ? 1 : -1);
  });
  const controls = document.querySelector(`[data-compass-controls="${id}"]`);
  controls?.querySelectorAll("[data-compass-step]").forEach((button) => {
    button.addEventListener("click", () => rotate(Number(button.dataset.compassStep)));
  });
  compass.render = render;
  rotaryCompasses.set(id, compass);
  render();
}

function setupRotaryCompasses() {
  document.querySelectorAll("[data-rotary-compass]").forEach(setupRotaryCompass);
  window.addEventListener("resize", () => rotaryCompasses.forEach((compass) => compass.render()));
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
  elements.authModeLogin.classList.toggle("active", !registering);
  elements.authModeRegister.classList.toggle("active", registering);
  elements.authModeLogin.setAttribute("aria-selected", String(!registering));
  elements.authModeRegister.setAttribute("aria-selected", String(registering));
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
    "Only public HTTPS recruitment pages can be watched.": "只支持公开的 HTTPS 企业机会页面。",
    "This address is not allowed for recruitment monitoring.": "这个地址不能加入哨站，请使用企业公开官网。",
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
  const pendingLaunch = state.pendingLaunch || state.activeProduct;
  state.pendingLaunch = null;
  if (pendingLaunch) window.setTimeout(() => launchProduct(pendingLaunch), 0);
  if (!state.user.privacy_accepted && !elements.consentDialog.open) {
    elements.consentDialog.showModal();
  } else if (!pendingLaunch) {
    window.setTimeout(openWorldMap, 80);
  }
}

async function loadHomeRecruitmentAlerts() {
  try {
    const [data, watchData] = await Promise.all([
      api("/recruitment/jobs"),
      api("/recruitment/watches").catch(() => ({ watches: [] })),
    ]);
    state.recruitmentJobs = data.jobs || [];
    state.recruitmentWatches = watchData.watches || watchData || [];
    const syncStatus = chatgptSyncFromJobs(data);
    if (syncStatus) renderRecruitmentSyncStatus(syncStatus);
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
  if (urgent.length) labels.push(`${urgent.length} 个关键时间窗正在收束`);
  if (changedWatches.length) labels.push(`${changedWatches.length} 个官网有变化`);
  elements.homeAlertTitle.textContent = labels.join(" · ");
  urgent.forEach((job) => {
    const link = document.createElement("a");
    link.href = job.url;
    link.target = "_blank";
    link.rel = "noreferrer";
    const urgency = job.days_left === 0 ? "今天截止" : `剩 ${job.days_left} 天`;
    link.append(
      makeElement("span", "", job.company || "机会发布方"),
      makeElement("strong", "", job.title || "机会信号"),
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
      makeElement("strong", "", watch.name || "公开信号页"),
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
  await soundscapeEngine.destroy();
  state.music.playing = false;
  state.music.minimized = false;
  state.music.status = state.music.enabled ? "resume_pending" : "idle";
  renderMusicUI();
  await storage.remove(STORAGE_KEYS.token);
  state.token = null;
  state.user = null;
  state.sessionId = null;
  state.sessions = [];
  state.documents = [];
  if (elements.settingsDialog.open) elements.settingsDialog.close();
  if (elements.consentDialog.open) elements.consentDialog.close();
  if (elements.worldMapDialog.open) elements.worldMapDialog.close();
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
  elements.worldMapCurrent.textContent = meta.themeName;
  document.querySelectorAll(".workspace-tab").forEach((button) => button.classList.toggle("active", button.dataset.workspace === state.workspace));
  document.querySelectorAll("[data-mobile-workspace]").forEach((button) => button.classList.toggle("active", button.dataset.mobileWorkspace === state.workspace));
  updateEvidence(state.latestEvidence.sources, state.latestEvidence.tools);
}

async function changeWorkspace(workspaceId) {
  if (workspaceId === state.workspace) return;
  state.workspace = workspaceId;
  state.sessionId = null;
  state.latestEvidence = { sources: [], tools: [] };
  state.activeProduct = workspaceId;
  await storage.set(STORAGE_KEYS.activeProduct, workspaceId);
  await storage.set(STORAGE_KEYS.workspace, workspaceId);
  await haptic();
  renderWorkspaceTabs();
  newConversation();
  playWorkspaceEntry(workspaceId);
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
    const previousWorkspace = state.workspace;
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
    if (state.workspace !== previousWorkspace) playWorkspaceEntry(state.workspace);
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
  if (!elements.crossExamDialog.open) {
    elements.crossExamDialog.showModal();
    playSceneEntry(elements.crossExamDialog);
  }
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
  if (!elements.studioDialog.open) {
    elements.studioDialog.showModal();
    playSceneEntry(elements.studioDialog);
  }
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
  if (!dialog.open) {
    dialog.showModal();
    playSceneEntry(dialog);
  }
}

function openWorldMap() {
  closePanels();
  if (!elements.worldMapDialog.open) {
    elements.worldMapDialog.showModal();
    playSceneEntry(elements.worldMapDialog);
    window.requestAnimationFrame(() => rotaryCompasses.get("world")?.render());
  }
}

function closeConcept(dialogId) {
  const dialog = $(dialogId);
  if (dialog?.open) dialog.close();
}

async function loadMusicPreferences() {
  const [enabled, volume, savedBlueprint] = await Promise.all([
    storage.get(STORAGE_KEYS.musicEnabled),
    storage.get(STORAGE_KEYS.musicVolume),
    storage.get(STORAGE_KEYS.musicBlueprint),
  ]);
  state.music.enabled = enabled === "true";
  const storedVolume = Number(volume);
  state.music.volume = volume !== null && Number.isFinite(storedVolume)
    ? Math.min(1, Math.max(0, storedVolume))
    : 0.18;
  let blueprint = null;
  try { blueprint = savedBlueprint ? JSON.parse(savedBlueprint) : null; } catch (_) {}
  applyMusicCreationTemplate(blueprint?.template || "cosmos", blueprint);
  if (state.music.enabled) {
    state.music.status = "resume_pending";
    state.music.currentTrack = blueprint?.title
      ? { id: "local-saved", name: blueprint.title, artistName: "由你创建" }
      : null;
  }
  renderMusicUI();
}

async function persistMusicPreferences() {
  const blueprint = currentMusicCreation();
  await Promise.all([
    storage.set(STORAGE_KEYS.musicEnabled, String(state.music.enabled)),
    storage.set(STORAGE_KEYS.musicVolume, String(state.music.volume)),
    storage.set(STORAGE_KEYS.musicBlueprint, JSON.stringify(blueprint)),
  ]);
}

function currentMusicCreation() {
  return {
    template: state.music.creationTemplate,
    title: elements.musicCreationTitle.value.trim(),
    style: elements.musicCreationStyle.value.trim(),
    mood: elements.musicCreationMood.value.trim(),
    tempo: Number(elements.musicCreationTempo.value),
    texture: elements.musicCreationTexture.value.trim(),
    description: elements.musicCreationDescription.value.trim(),
    scale: state.music.creationScale,
    instruments: [...document.querySelectorAll("[data-music-instrument]:checked")].map((input) => input.dataset.musicInstrument),
    vocalProfile: elements.musicVocalProfile.value,
  };
}

function applyMusicCreationTemplate(templateId, overrides = null) {
  const template = MUSIC_CREATION_TEMPLATES[templateId] || MUSIC_CREATION_TEMPLATES.cosmos;
  state.music.creationTemplate = templateId in MUSIC_CREATION_TEMPLATES ? templateId : "cosmos";
  state.music.creationScale = overrides?.scale || template.scale;
  state.music.creationInstruments = overrides?.instruments || template.instruments;
  state.music.vocalProfile = overrides?.vocalProfile || template.vocalProfile;
  elements.musicCreationTitle.value = overrides?.title || template.title;
  elements.musicCreationStyle.value = overrides?.style || template.style;
  elements.musicCreationMood.value = overrides?.mood || template.mood;
  elements.musicCreationTempo.value = String(overrides?.tempo || template.tempo);
  elements.musicCreationTexture.value = overrides?.texture || template.texture;
  elements.musicCreationDescription.value = overrides?.description || "";
  elements.musicVocalProfile.value = state.music.vocalProfile;
  document.querySelectorAll("[data-music-instrument]").forEach((input) => {
    input.checked = state.music.creationInstruments.includes(input.dataset.musicInstrument);
  });
  elements.musicTempoOutput.textContent = `${elements.musicCreationTempo.value} BPM`;
  document.querySelectorAll("[data-music-template]").forEach((button) => {
    button.classList.toggle("active", button.dataset.musicTemplate === state.music.creationTemplate);
  });
  rewriteMusicBlueprint();
}

function rewriteMusicBlueprint() {
  elements.musicBlueprint.value = buildMusicBlueprint(currentMusicCreation());
  return elements.musicBlueprint.value;
}

function musicStatusCopy() {
  if (state.music.loading) return "正在编排乐器与原创声线…";
  const copies = {
    idle: "选择一个模板，让八度空间开始转动。",
    resume_pending: "上次的声音蓝图已保留，点击重新生成并播放。",
    generated_playing: "你的原创编曲正在本机运行。",
    generated_paused: "时间停在这一拍。",
    playback_failed: "当前浏览器暂时无法生成声音，请稍后重试。",
  };
  return state.music.error || copies[state.music.status] || copies.idle;
}

function renderMusicUI() {
  if (!elements.musicMiniPlayer) return;
  const hasPlayback = state.music.enabled && Boolean(state.music.currentTrack);
  const currentTrack = state.music.currentTrack;
  const statusCopy = musicStatusCopy();

  document.body.dataset.musicEnabled = String(state.music.enabled);
  elements.musicMiniPlayer.classList.toggle("hidden", !state.music.minimized);
  elements.musicMiniPlayer.classList.toggle("is-idle", !state.music.enabled);
  elements.musicMiniPlayer.classList.toggle("is-active", state.music.enabled);
  elements.musicMiniPlayer.classList.toggle("is-playing", state.music.playing);
  elements.musicMiniTrack.textContent = state.music.enabled
    ? (currentTrack?.name || "点击继续创作")
    : "八度空间";
  elements.musicMiniToggle.textContent = state.music.playing ? "Ⅱ" : "▶";
  elements.musicMiniToggle.disabled = !hasPlayback || state.music.loading;
  elements.musicMiniMute.disabled = !hasPlayback;
  elements.musicMiniMute.textContent = state.music.muted || state.music.volume === 0 ? "○" : "◖";

  elements.musicCurrentTrack.textContent = currentTrack?.name || "尚未生成声音";
  elements.musicCurrentArtist.textContent = currentTrack?.artistName || "由你定义";
  elements.musicStatus.querySelector("span").textContent = statusCopy;
  elements.musicStatus.dataset.status = state.music.status;
  elements.musicPlayToggle.disabled = !hasPlayback || state.music.loading;
  elements.musicPlayToggle.textContent = state.music.playing ? "Ⅱ" : "▶";
  elements.musicVolume.value = String(state.music.volume);
  elements.musicVolumeOutput.textContent = `${Math.round(state.music.volume * 100)}%`;
  elements.musicEnable.disabled = state.music.loading;
  elements.musicEnable.querySelector("span").textContent = state.music.loading
    ? "正在生成…"
    : state.music.playing ? "重新生成这个作品" : "生成并播放我的作品";
}

function openMusicDimension() {
  closePanels();
  state.music.minimized = false;
  if (!elements.musicDialog.open) {
    elements.musicDialog.showModal();
    playSceneEntry(elements.musicDialog);
    const shell = elements.musicDialog.querySelector(".music-dimension-shell");
    window.requestAnimationFrame(() => { if (shell) shell.scrollTop = 0; });
  }
  renderMusicUI();
}

async function minimizeMusicDimension() {
  if (elements.musicDialog.open) elements.musicDialog.close();
  state.music.minimized = true;
  renderMusicUI();
}

async function closeMusicDimension() {
  if (elements.musicDialog.open) elements.musicDialog.close();
  await disableMusicDimension();
}

async function copyMusicBlueprint() {
  const blueprint = rewriteMusicBlueprint();
  try {
    await navigator.clipboard.writeText(blueprint);
    showToast("创作描述已复制。", 2200);
  } catch (_) {
    elements.musicBlueprint.focus();
    elements.musicBlueprint.select();
    showToast("已选中创作描述，请手动复制。", 2800);
  }
}

async function generateLocalSoundscape() {
  if (state.music.loading) return;
  state.music.enabled = true;
  state.music.loading = true;
  state.music.source = "local_composer";
  state.music.error = null;
  rewriteMusicBlueprint();
  renderMusicUI();
  try {
    const creation = currentMusicCreation();
    await soundscapeEngine.start({ ...creation, volume: state.music.volume });
    state.music.currentTrack = {
      id: `local-${Date.now()}`,
      name: creation.title || "未命名原创作品",
      artistName: "本地编曲 · 由你创建",
    };
    state.music.playing = true;
    state.music.status = "generated_playing";
    await persistMusicPreferences();
  } catch (error) {
    state.music.status = "playback_failed";
    state.music.error = error.message || "当前浏览器暂时无法生成声音。";
    state.music.playing = false;
  } finally {
    state.music.loading = false;
    renderMusicUI();
  }
}

async function toggleMusicPlayback() {
  if (!state.music.enabled || !state.music.currentTrack) {
    openMusicDimension();
    return;
  }
  try {
    if (state.music.playing) await soundscapeEngine.pause();
    else await soundscapeEngine.resume();
    state.music.playing = !state.music.playing;
    state.music.status = state.music.playing ? "generated_playing" : "generated_paused";
  } catch (error) {
    state.music.status = error.code || "playback_failed";
    state.music.error = error.message;
    state.music.playing = false;
  }
  renderMusicUI();
}

async function changeMusicVolume(value) {
  state.music.volume = Math.min(1, Math.max(0, Number(value)));
  state.music.muted = false;
  soundscapeEngine.setVolume(state.music.volume);
  await persistMusicPreferences();
  renderMusicUI();
}

async function toggleMusicMute() {
  state.music.muted = !state.music.muted;
  soundscapeEngine.setVolume(state.music.muted ? 0 : state.music.volume);
  renderMusicUI();
}

async function disableMusicDimension() {
  if (elements.musicDialog.open) elements.musicDialog.close();
  await soundscapeEngine.destroy();
  state.music.enabled = false;
  state.music.playing = false;
  state.music.error = null;
  state.music.status = "idle";
  state.music.currentTrack = null;
  state.music.minimized = false;
  state.music.source = "local_composer";
  await persistMusicPreferences();
  renderMusicUI();
}

function photonSourceMode() {
  return document.querySelector('input[name="photon-source"]:checked')?.value || "inspiration";
}

function photonTrackId() {
  return document.querySelector('input[name="photon-track"]:checked')?.value || "text";
}

function photonStyles() {
  return Object.fromEntries(Object.keys(PHOTON_STYLE_META).map((key) => {
    const value = Number($(`photon-${key}`)?.value || 50);
    return [key, Math.min(100, Math.max(0, value))];
  }));
}

function photonStyleLine(key, value) {
  const [left, right] = PHOTON_STYLE_META[key];
  if (value < 45) return `${left} ${100 - value}`;
  if (value > 55) return `${right} ${value}`;
  return `${left}/${right} 平衡`;
}

function photonStyleSummary(styles = photonStyles()) {
  return Object.entries(styles).map(([key, value]) => photonStyleLine(key, value)).join(" · ");
}

function renderPhotonWorldOptions() {
  const selected = elements.photonWorldSelect.value;
  elements.photonWorldSelect.replaceChildren();
  if (!state.spaces.length) {
    const option = makeElement("option", "", "还没有可用的世界");
    option.value = "";
    elements.photonWorldSelect.appendChild(option);
    elements.photonWorldNote.textContent = "先在造界创建世界，或切回自由灵感。";
    return;
  }
  state.spaces.forEach((space) => {
    const option = makeElement("option", "", space.name);
    option.value = space.id;
    elements.photonWorldSelect.appendChild(option);
  });
  if (state.spaces.some((space) => space.id === selected)) elements.photonWorldSelect.value = selected;
  elements.photonWorldNote.textContent = "只使用名称、描述与有限世界规则；不读取聊天记录、文档或运行历史。";
}

async function updatePhotonSourceMode() {
  const fromWorld = photonSourceMode() === "world";
  elements.photonWorldField.classList.toggle("hidden", !fromWorld);
  if (!fromWorld) return;
  elements.photonError.textContent = "";
  try {
    if (!state.spaces.length) await refreshStudio();
    renderPhotonWorldOptions();
  } catch (error) {
    elements.photonError.textContent = `世界读取失败：${translateError(error.message)}`;
    renderPhotonWorldOptions();
  }
}

function selectedPhotonWorld() {
  if (photonSourceMode() !== "world") return null;
  return state.spaces.find((space) => space.id === elements.photonWorldSelect.value) || null;
}

function collectPhotonContext() {
  const sourceMode = photonSourceMode();
  const world = selectedPhotonWorld();
  const input = elements.photonInspiration.value.trim();
  if (sourceMode === "inspiration" && !input) throw new Error("请先放入一束灵感。");
  if (sourceMode === "world" && !world) throw new Error("请选择一个已有世界，或切回自由灵感。");
  const trackId = photonTrackId();
  return {
    sourceMode,
    sourceLabel: world ? world.name : "自由灵感",
    world: world ? {
      name: String(world.name || "").slice(0, 60),
      description: String(world.description || "").slice(0, 360),
      rules: String(world.system_prompt || world.rules || "").slice(0, 600),
    } : null,
    input,
    trackId,
    track: PHOTON_TRACKS[trackId] || PHOTON_TRACKS.text,
    styles: photonStyles(),
  };
}

function photonCreationTitle(output, trackLabel) {
  const firstLine = String(output || "").split("\n")
    .map((line) => line.replace(/^\s*[#>*_`\-\d.)]+\s*/, "").trim())
    .find(Boolean);
  return (firstLine || `${trackLabel}显影`).slice(0, 60);
}

function renderPhotonResult(creation) {
  state.photon.current = creation;
  elements.photonResultMode.textContent = creation.mode === "local" ? "LOCAL SKELETON · 0 TOKEN" : "AI PROJECTION · 1 REQUEST";
  elements.photonResultTrack.textContent = PHOTON_TRACKS[creation.track]?.label || creation.track;
  elements.photonResultSource.textContent = creation.sourceLabel;
  elements.photonResultStyle.textContent = photonStyleSummary(creation.styles);
  elements.photonResultUsage.textContent = creation.usage === null || creation.usage === undefined ? "接口未返回" : String(creation.usage);
  elements.photonResultBody.classList.remove("empty", "loading");
  elements.photonResultBody.innerHTML = safeMarkdown(creation.output);
  elements.photonCopy.disabled = false;
  elements.photonSave.disabled = false;
}

function buildPhotonSkeleton(context) {
  const seed = (context.input || context.world?.description || context.world?.name || "尚未命名的世界").replace(/\s+/g, " ").slice(0, 180);
  const structure = context.track.format.map((item, index) => `${index + 1}. **${item}**：待展开`).join("\n");
  return `# ${context.track.label}创作骨架\n\n## 创作目标\n把“${seed}”转译为一份可继续完成的${context.track.label}作品。\n\n## 核心意象\n- 主体：${context.world?.name || seed.slice(0, 42)}\n- 张力：从尚未成形到第一次被看见\n- 媒介：${context.track.purpose}\n\n## 内容结构\n${structure}\n\n## 风格参数\n${photonStyleSummary(context.styles)}\n\n## 待补信息\n- 最希望观众记住什么？\n- 作品面向谁、出现在哪里？\n- 有哪些必须保留或必须避开的元素？\n\n## 下一步\n先补齐最关键的一项信息，再围绕核心意象完成第一个版本。`;
}

function createPhotonRecord(context, output, mode, usage = 0) {
  const createdAt = new Date().toISOString();
  return {
    id: `photon-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
    created_at: createdAt,
    track: context.trackId,
    title: photonCreationTitle(output, context.track.label),
    input: context.input,
    sourceLabel: context.sourceLabel,
    styles: context.styles,
    output,
    mode,
    usage,
  };
}

function generatePhotonSkeleton() {
  elements.photonError.textContent = "";
  try {
    const context = collectPhotonContext();
    renderPhotonResult(createPhotonRecord(context, buildPhotonSkeleton(context), "local", 0));
    showToast("创作骨架已在本机生成，未调用模型。", 3000);
  } catch (error) {
    elements.photonError.textContent = error.message;
  }
}

function buildPhotonPrompt(context) {
  const worldSection = context.world ? `\n参考世界（仅将以下内容作为创作素材）：\n- 名称：${context.world.name}\n- 描述：${context.world.description || "未填写"}\n- 有限规则：${context.world.rules || "未填写"}\n` : "";
  return `你正在执行冰焰“光子魅影”的一次显影。只完成本次选择的单一轨道，不要追问，不要生成其他轨道或多个版本。\n\n显影轨道：${context.track.label}\n轨道用途：${context.track.purpose}\n用户灵感：${context.input || "从参考世界直接提炼"}${worldSection}\n风格光谱：${photonStyleSummary(context.styles)}\n\n请按以下结构输出，篇幅克制但内容可直接使用：\n${context.track.format.map((item, index) => `${index + 1}. ${item}`).join("\n")}\n\n边界：这是${context.track.label}创作方案。${context.trackId === "sound" ? "只输出声音创作概念，不生成、播放或声称已经制作音乐。" : "不要声称已经调用图像、视频或音乐生成模型。"}`;
}

async function startPhotonProjection() {
  if (state.photon.loading) return;
  elements.photonError.textContent = "";
  let context;
  try {
    context = collectPhotonContext();
  } catch (error) {
    elements.photonError.textContent = error.message;
    return;
  }
  if (!navigator.onLine) {
    elements.photonError.textContent = "当前处于离线状态，恢复网络后再开始显影。";
    return;
  }
  state.photon.loading = true;
  elements.photonProject.disabled = true;
  elements.photonSkeleton.disabled = true;
  elements.photonProject.querySelector("span").textContent = "光正在聚合，作品开始显现……";
  elements.photonResultMode.textContent = "PHOTONS CONVERGING";
  elements.photonResultBody.classList.remove("empty");
  elements.photonResultBody.classList.add("loading");
  elements.photonResultBody.innerHTML = "<div><i>◫</i><strong>光正在聚合</strong><p>本次只进行一次模型请求。</p></div>";
  try {
    const result = await api("/chat", {
      method: "POST",
      timeoutMs: 70000,
      body: JSON.stringify({ message: buildPhotonPrompt(context), session_id: null, workspace: "general", creative_single_pass: true }),
    });
    const usage = result.usage?.total_tokens ?? null;
    renderPhotonResult(createPhotonRecord(context, result.reply || "模型没有返回内容。", "ai", usage));
    loadSessions().catch(() => {});
    await haptic();
  } catch (error) {
    elements.photonResultBody.classList.remove("loading");
    elements.photonResultBody.classList.add("empty");
    elements.photonResultBody.innerHTML = "<div><i>◫</i><strong>显影未完成</strong><p>灵感仍保留在输入区，可以稍后重试。</p></div>";
    elements.photonError.textContent = translateError(error.message);
  } finally {
    state.photon.loading = false;
    elements.photonProject.disabled = false;
    elements.photonSkeleton.disabled = false;
    elements.photonProject.querySelector("span").textContent = "开始显影";
  }
}

async function copyPhotonResult() {
  if (!state.photon.current?.output) return;
  try {
    await navigator.clipboard.writeText(state.photon.current.output);
    showToast("显影结果已复制。", 2200);
  } catch (_) {
    showToast("当前浏览器未允许自动复制。", 2600);
  }
}

function renderPhotonHistory() {
  elements.photonHistoryList.replaceChildren();
  if (!state.photon.creations.length) {
    elements.photonHistoryList.appendChild(makeElement("p", "", "还没有本地作品。"));
    return;
  }
  state.photon.creations.forEach((creation) => {
    const row = makeElement("article", "photon-history-item");
    const open = makeElement("button", "photon-history-open");
    open.type = "button";
    const copy = makeElement("span");
    copy.append(makeElement("strong", "", creation.title), makeElement("small", "", `${PHOTON_TRACKS[creation.track]?.label || creation.track} · ${new Date(creation.created_at).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}`));
    open.append(makeElement("i", "", "◫"), copy);
    open.addEventListener("click", () => renderPhotonResult(creation));
    const remove = makeElement("button", "photon-history-delete", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `删除${creation.title}`);
    remove.addEventListener("click", () => deletePhotonCreation(creation.id));
    row.append(open, remove);
    elements.photonHistoryList.appendChild(row);
  });
}

async function loadPhotonCreations() {
  if (state.photon.creationsLoaded) return renderPhotonHistory();
  let saved = [];
  try { saved = JSON.parse((await storage.get(STORAGE_KEYS.photonCreations)) || "[]"); } catch (_) {}
  state.photon.creations = Array.isArray(saved) ? saved.slice(0, 10) : [];
  state.photon.creationsLoaded = true;
  renderPhotonHistory();
}

async function savePhotonCreation() {
  const creation = state.photon.current;
  if (!creation) return;
  state.photon.creations = [creation, ...state.photon.creations.filter((item) => item.id !== creation.id)].slice(0, 10);
  await storage.set(STORAGE_KEYS.photonCreations, JSON.stringify(state.photon.creations));
  renderPhotonHistory();
  showToast("作品已保存到当前设备。", 2400);
}

async function deletePhotonCreation(id) {
  state.photon.creations = state.photon.creations.filter((item) => item.id !== id);
  await storage.set(STORAGE_KEYS.photonCreations, JSON.stringify(state.photon.creations));
  renderPhotonHistory();
}

async function openPhotonProjection() {
  closePanels();
  elements.photonError.textContent = "";
  if (!elements.photonDialog.open) {
    elements.photonDialog.showModal();
    playSceneEntry(elements.photonDialog);
    window.requestAnimationFrame(() => { elements.photonDialog.querySelector(".photon-projection-shell").scrollTop = 0; });
  }
  await loadPhotonCreations();
  if (photonSourceMode() === "world") await updatePhotonSourceMode();
}

async function launchProduct(product) {
  state.activeProduct = product;
  await storage.set(STORAGE_KEYS.activeProduct, product);
  if (elements.worldMapDialog.open) elements.worldMapDialog.close();
  if (product === "resonance") return openConcept(elements.resonanceDialog);
  if (product === "trace") return openConcept(elements.traceDialog);
  if (product === "oblivion") return openOblivionArchive();
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
    else playWorkspaceEntry(product);
    return;
  }
  if (product === "recruitment") return openRecruitment();
  if (product === "forge") return openStudio();
  if (product === "music") return openMusicDimension();
  if (product === "photon") return openPhotonProjection();
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

function valueAtPaths(source, paths) {
  for (const path of paths) {
    const value = path.split(".").reduce((current, key) => current?.[key], source);
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return null;
}

function syncCount(source, paths, sources, sourcePaths) {
  const directValue = valueAtPaths(source, paths);
  const direct = Number(directValue);
  if (directValue !== null && Number.isFinite(direct) && direct >= 0) return direct;
  let found = false;
  const total = (sources || []).reduce((sum, item) => {
    const rawValue = valueAtPaths(item, sourcePaths);
    const value = Number(rawValue);
    if (rawValue === null || !Number.isFinite(value) || value < 0) return sum;
    found = true;
    return sum + value;
  }, 0);
  return found ? total : null;
}

function formatSyncTime(value, fallback = "等待首次同步") {
  if (!value) return fallback;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    year: parsed.getFullYear() === new Date().getFullYear() ? undefined : "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(parsed);
}

function chatgptSyncFromJobs(data) {
  return data?.data_status?.chatgpt_sync
    || data?.chatgpt_sync
    || data?.data_status?.sync?.chatgpt
    || null;
}

function ensureRecruitmentSyncPanel() {
  const existing = $("recruitment-chatgpt-sync");
  if (existing) return existing;

  const panel = document.createElement("section");
  panel.id = "recruitment-chatgpt-sync";
  panel.className = "recruitment-sync-panel";
  panel.setAttribute("role", "status");
  panel.setAttribute("aria-live", "polite");

  const header = makeElement("div", "recruitment-sync-header");
  const identity = makeElement("div", "recruitment-sync-identity");
  const orbit = makeElement("span", "recruitment-sync-orbit");
  orbit.setAttribute("aria-hidden", "true");
  orbit.append(...Array.from({ length: CHATGPT_MONITOR_SOURCE_COUNT }, () => makeElement("i", "unknown")));
  const title = document.createElement("div");
  const eyebrow = makeElement("small", "", "CONTROLLED CHAT BRIDGE");
  const heading = makeElement("strong", "", `${CHATGPT_MONITOR_SOURCE_COUNT} 个 ChatGPT 监控源`);
  heading.dataset.syncTitle = "true";
  title.append(eyebrow, heading);
  identity.append(orbit, title);
  const badge = makeElement("span", "recruitment-sync-badge pending", "等待同步");
  badge.dataset.syncBadge = "true";
  header.append(identity, badge);

  const description = makeElement(
    "p",
    "recruitment-sync-description",
    "由受控同步桥读取指定监控对话，只向冰焰提交结构化机会信号；这不是 ChatGPT API 直连。",
  );
  const metrics = makeElement("div", "recruitment-sync-metrics");
  [
    ["最后同步", "last-sync", "等待首次同步"],
    ["已核验", "accepted", "—"],
    ["待核验", "pending", "—"],
    ["已拒绝", "rejected", "—"],
  ].forEach(([label, key, value]) => {
    const metric = document.createElement("article");
    metric.dataset.syncMetric = key;
    metric.append(makeElement("small", "", label), makeElement("strong", "", value));
    metrics.appendChild(metric);
  });
  const footer = makeElement("footer", "recruitment-sync-footer");
  footer.append(
    makeElement("span", "", "受控桥接 · 非 ChatGPT API 直连"),
    makeElement("b", "", "状态待后端回报"),
  );
  panel.append(header, description, metrics, footer);

  const disclaimer = document.querySelector(".recruitment-results .recruitment-disclaimer");
  if (disclaimer?.parentNode) disclaimer.parentNode.insertBefore(panel, disclaimer);
  return panel;
}

function renderRecruitmentSyncStatus(rawStatus) {
  const panel = ensureRecruitmentSyncPanel();
  const status = rawStatus && typeof rawStatus === "object" ? rawStatus : null;
  const sources = Array.isArray(status?.sources) ? status.sources : [];
  const expectedRaw = Number(valueAtPaths(status, ["expected_source_count", "expected_sources", "configured_source_count"]));
  const expected = Number.isFinite(expectedRaw) && expectedRaw > 0 ? expectedRaw : CHATGPT_MONITOR_SOURCE_COUNT;
  const connectedValue = valueAtPaths(status, ["connected_source_count", "active_source_count", "source_count"]);
  const connected = connectedValue !== null && Number.isFinite(Number(connectedValue)) && Number(connectedValue) >= 0
    ? Number(connectedValue)
    : (sources.length || null);
  const lastSyncedAt = valueAtPaths(status, [
    "last_synced_at", "last_sync_at", "last_completed_at", "completed_at", "updated_at",
  ]);
  const latestAccepted = syncCount(status, [
    "accepted", "verified", "accepted_count", "verified_count", "counts.accepted", "counts.verified",
  ], [], []);
  const latestPending = syncCount(status, [
    "pending", "pending_count", "pending_verification", "pending_verification_count", "counts.pending",
  ], [], []);
  const latestRejected = syncCount(status, [
    "rejected", "skipped", "rejected_count", "skipped_count", "counts.rejected", "counts.skipped",
  ], [], []);
  const sourceAccepted = syncCount(null, [], sources, ["accepted", "verified", "accepted_count", "verified_count"]);
  const sourcePending = syncCount(null, [], sources, ["pending", "pending_count", "pending_verification"]);
  const sourceRejected = syncCount(null, [], sources, ["rejected", "skipped", "rejected_count", "skipped_count"]);
  const inventoryAccepted = syncCount(status, ["inventory_accepted", "counts.inventory_accepted"], [], [])
    ?? latestAccepted
    ?? sourceAccepted;
  const inventoryPending = syncCount(status, ["inventory_pending", "counts.inventory_pending"], [], [])
    ?? latestPending
    ?? sourcePending;
  const inventoryRejected = syncCount(status, ["inventory_rejected", "counts.inventory_rejected"], [], [])
    ?? latestRejected
    ?? sourceRejected;
  const rawState = String(valueAtPaths(status, ["status", "state", "bridge_status"]) || "").toLowerCase();
  const isRunning = /running|syncing|in_progress/.test(rawState);
  const isError = /error|failed|unavailable/.test(rawState);
  const isDisabled = /disabled|paused|inactive/.test(rawState);
  const allSourcesConnected = connected != null && connected >= expected;
  const noReviewBacklog = (latestPending == null || latestPending === 0)
    && (latestRejected == null || latestRejected === 0);
  const isSynced = rawState === "synced" && allSourcesConnected && Boolean(lastSyncedAt) && noReviewBacklog;
  const isPartial = rawState === "partial"
    || (connected != null && connected > 0 && !isSynced)
    || (latestPending != null && latestPending > 0)
    || (latestRejected != null && latestRejected > 0);
  const visualState = isRunning
    ? "running"
    : isError
      ? "error"
      : isDisabled
        ? "disabled"
        : isSynced
          ? "synced"
          : isPartial
            ? "partial"
            : "pending";
  const badgeCopy = {
    running: "正在同步",
    error: "同步异常",
    disabled: "桥接暂停",
    synced: "同步完成",
    partial: "部分同步",
    pending: "等待同步",
  }[visualState];

  panel.dataset.state = visualState;
  panel.querySelector("[data-sync-title]").textContent = `${expected} 个 ChatGPT 监控源`;
  const badge = panel.querySelector("[data-sync-badge]");
  badge.className = `recruitment-sync-badge ${visualState}`;
  badge.textContent = badgeCopy;
  const metricValues = {
    "last-sync": formatSyncTime(lastSyncedAt),
    accepted: inventoryAccepted == null ? "—" : Number(inventoryAccepted).toLocaleString("zh-CN"),
    pending: inventoryPending == null ? "—" : Number(inventoryPending).toLocaleString("zh-CN"),
    rejected: inventoryRejected == null ? "—" : Number(inventoryRejected).toLocaleString("zh-CN"),
  };
  Object.entries(metricValues).forEach(([key, value]) => {
    const target = panel.querySelector(`[data-sync-metric="${key}"] strong`);
    if (target) target.textContent = value;
  });

  const nodes = [...panel.querySelectorAll(".recruitment-sync-orbit i")];
  nodes.forEach((node, index) => {
    const source = sources[index];
    const sourceState = String(source?.status || source?.state || "").toLowerCase();
    node.className = source
      ? (/error|failed|rejected/.test(sourceState)
        ? "error"
        : (!source.last_seen_at || /pending|waiting|new/.test(sourceState) ? "pending" : "active"))
      : (status && connected != null && index < connected
        ? (visualState === "error" ? "error" : visualState === "pending" ? "pending" : "active")
        : "unknown");
    node.title = source?.title || source?.name || `监控源 ${index + 1}`;
  });
  const footerStatus = panel.querySelector(".recruitment-sync-footer b");
  footerStatus.textContent = !status
    ? "状态待后端回报"
    : connected == null
      ? `${expected} 个配置目标`
      : visualState === "synced"
        ? `${expected} / ${expected} 已同步`
        : `${Math.min(connected, expected)} / ${expected} 源已回传`;
  state.recruitmentSyncStatus = status;
}

function formatHistoricalAiSearch(webSearch, triggeredThisRun = false) {
  if (!webSearch || typeof webSearch !== "object") return "AI 补漏尚无历史运行记录";
  const status = String(webSearch.status || "").toLowerCase();
  const timestamp = webSearch.completed_at || webSearch.last_attempt_at || webSearch.updated_at || webSearch.last_run_at;
  const label = triggeredThisRun ? "本次 AI 补漏" : "上次 AI 补漏";
  const timeCopy = timestamp ? `（${formatSyncTime(timestamp, "")}）` : "";
  if (status === "success" || status === "completed") {
    const jobsValue = webSearch.jobs ?? webSearch.count ?? webSearch.candidates;
    const jobs = Number(jobsValue);
    return jobsValue == null || !Number.isFinite(jobs)
      ? `${label}${timeCopy}已完成`
      : `${label}${timeCopy}发现 ${jobs.toLocaleString("zh-CN")} 条候选`;
  }
  if (status === "error" || status === "failed") return `${label}${timeCopy}未完成`;
  if (status === "running" || status === "in_progress") return `AI 补漏正在独立后台运行${timeCopy}`;
  return `${label}${timeCopy}暂无可展示结果`;
}

function formatDeepSearchOutcome(sourceResult) {
  const webSearch = sourceResult?.web_search;
  const ranThisTime = sourceResult?.web_search_ran === true
    || sourceResult?.web_search_triggered === true
    || webSearch?.triggered_this_run === true;
  if (ranThisTime) return formatHistoricalAiSearch(webSearch, true);
  const nextDueCopy = sourceResult?.next_due_at
    ? `，${formatSyncTime(sourceResult.next_due_at, "稍后")} 后可再次运行`
    : "";
  const skipCopy = {
    deep_search_cooldown: `AI 补漏：15 分钟冷却中，本次未调用模型${nextDueCopy}`,
    web_search_disabled: "AI 补漏：服务端未启用，本次未调用模型",
    web_search_not_started: "AI 补漏：本次未能启动，未将历史结果计作本轮",
    deep_search_not_requested: "AI 补漏：本次未请求模型",
  }[sourceResult?.skip_reason];
  return skipCopy || `${formatHistoricalAiSearch(webSearch, false)}；本次未调用模型`;
}

function renderRecruitmentProfile(profile) {
  if (!profile) return;
  elements.recruitmentRoles.value = (profile.desired_roles || []).join("，");
  elements.recruitmentIndustries.value = (profile.industries || []).join("，");
  elements.recruitmentLocations.value = (profile.locations || []).join("，");
  const selectedEmployerTypes = new Set(profile.employer_types || []);
  document.querySelectorAll(".recruitment-checks input").forEach((input) => {
    input.checked = selectedEmployerTypes.has(input.value)
      || (input.value === "快消/外企/咨询"
        && (selectedEmployerTypes.has("快消/消费") || selectedEmployerTypes.has("外企/咨询")));
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
    elements.recruitmentWatchList.appendChild(makeElement("small", "", "尚未建立企业信号哨站。填写企业名称，即可追踪公开机会池的变化。"));
    return;
  }
  watches.forEach((watch) => {
    const card = document.createElement("article");
    card.className = `watch-card${watchHasFreshChange(watch) ? " changed" : ""}`;
    const top = makeElement("div", "watch-card-top");
    const copy = document.createElement("div");
    if (watch.watch_type === "company") {
      copy.append(
        makeElement("strong", "", watch.company_name || watch.name || "企业机会池"),
        makeElement("small", "watch-target-copy", "公开机会信号追踪"),
      );
    } else {
      const link = makeElement("a", "", watch.url || "");
      link.href = watch.url;
      link.target = "_blank";
      link.rel = "noreferrer";
      copy.append(makeElement("strong", "", watch.name || "企业公开页面"), link);
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
        ? (keywordList.length ? `已在机会池中追踪：${keywordList.join(" · ")}` : "新机会进入信号池后自动提示。")
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
  const reviewJobs = jobs.filter((job) => (job.tags || []).includes("待官方核验"));
  const verifiedJobs = jobs.filter((job) => !(job.tags || []).includes("待官方核验"));
  const urgent = verifiedJobs.filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0 && job.days_left <= 7);
  const dated = verifiedJobs.filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0);
  const heading = document.createElement("strong");
  heading.textContent = urgent.length
    ? `时间窗预警 · ${urgent.length} 个已核验机会将在 7 天内关闭`
    : dated.length
      ? "时间窗预警 · 暂无 7 天内关闭的已核验机会"
      : "时间窗预警 · 暂无原始公告明确标注截止日期，刷新后将自动核验";
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
  if (reviewJobs.length) {
    const note = document.createElement("small");
    note.className = "recruitment-review-note";
    note.textContent = `另有 ${reviewJobs.length} 个候选信号保留在列表中；名称尚未被原始公告正文确认，因此不进入时间窗预警。`;
    elements.recruitmentDeadlineAlerts.append(note);
  }
}

function selectedRecruitmentStarfields() {
  return [...document.querySelectorAll(".recruitment-checks input:checked")].map((input) => input.value);
}

function fallbackRecruitmentCategories(job) {
  const text = `${job.company || ""} ${job.title || ""} ${job.employer_type || ""} ${job.industry || ""}`.toLocaleLowerCase("zh-CN");
  const categories = [];
  const rules = [
    ["央国企", ["石油", "石化", "海油", "国家电网", "国家能源", "华能", "华电", "大唐", "国家电投", "中核", "中化", "中粮", "五矿", "中铝"]],
    ["央国企科技", ["中国移动", "中国电信", "中国联通", "中国铁塔", "中国电科", "中国电子", "航天", "航空工业", "中国商飞", "中国铁路", "中国船舶", "兵器"]],
    ["烟草/专卖", ["烟草", "中烟", "烟草专卖"]],
    ["银行/金融", ["人民银行", "国家开发银行", "进出口银行", "农业发展银行", "工商银行", "农业银行", "中国银行", "建设银行", "交通银行", "邮储银行"]],
    ["券商/基金", ["证券", "基金", "资管", "资产管理", "期货", "投行"]],
    ["保险/综合金融", ["保险", "人保", "人寿", "太平", "平安", "再保险"]],
    ["互联网企业", ["腾讯", "阿里", "字节", "百度", "拼多多", "美团", "京东", "小米", "网易", "快手", "滴滴", "携程", "华为", "大疆", "互联网", "云计算"]],
    ["快消/外企/咨询", ["宝洁", "联合利华", "欧莱雅", "雀巢", "玛氏", "可口可乐", "百事", "咨询", "kearney", "科尔尼", "麦肯锡", "德勤", "普华永道", "毕马威", "安永", "microsoft", "google", "apple", "nvidia", "hsbc", "汇丰"]],
    ["量化私募", ["量化", "私募", "对冲基金", "point72", "幻方", "明汯", "衍复", "灵均", "宽德"]],
  ];
  rules.forEach(([category, markers]) => {
    if (markers.some((marker) => text.includes(marker))) categories.push(category);
  });
  return categories;
}

function filterRecruitmentByStarfield(jobs) {
  const selected = selectedRecruitmentStarfields();
  if (!selected.length) return jobs;
  return jobs.filter((job) => {
    const categories = Array.isArray(job.employer_categories) && job.employer_categories.length
      ? job.employer_categories
      : fallbackRecruitmentCategories(job);
    return selected.some((category) => categories.includes(category));
  });
}

function renderRecruitmentJobs(jobs) {
  elements.recruitmentJobs.replaceChildren();
  const starfieldJobs = filterRecruitmentByStarfield(jobs);
  if (!starfieldJobs.length) {
    elements.recruitmentJobs.innerHTML = '<div class="empty-list">当前筛选条件下没有达到 T3 以上的可投机会。减少一项筛选条件，或刷新公开信源后再试。</div>';
    return;
  }
  const availableJobs = starfieldJobs.filter((job) => /^https:\/\//.test(job.url || ""));
  const tierDefinitions = [
    ["T0", "终极目标", "90–100"], ["T0.5", "准终极", "85–89"],
    ["T1", "核心主申", "80–84"], ["T1.5", "高质量重点", "75–79"],
    ["T2", "值得申请", "70–74"], ["T2.5", "稳健补充", "65–69"], ["T3", "低优先级", "60–64"],
  ];
  const tierOrder = tierDefinitions.map(([code]) => code);
  const tierSummary = makeElement("div", "job-tier-summary");
  const allButton = makeElement("button", "job-tier-summary-item ALL", "");
  allButton.type = "button";
  allButton.dataset.tier = "ALL";
  allButton.setAttribute("aria-pressed", String(state.recruitmentTierFilter === "ALL"));
  allButton.classList.toggle("active", state.recruitmentTierFilter === "ALL");
  allButton.append(makeElement("b", "", "全部"), makeElement("small", "", `${availableJobs.length} 个`));
  allButton.addEventListener("click", () => {
    state.recruitmentTierFilter = "ALL";
    renderRecruitmentJobs(jobs);
  });
  tierSummary.appendChild(allButton);
  tierDefinitions.forEach(([tier, label, range]) => {
    const count = availableJobs.filter((job) => job.tier_code === tier).length;
    const tierClass = tier.replace(".", "-");
    const button = makeElement("button", `job-tier-summary-item ${tierClass}`);
    button.type = "button";
    button.dataset.tier = tier;
    button.title = `${label}：综合分 ${range}`;
    button.setAttribute("aria-pressed", String(state.recruitmentTierFilter === tier));
    button.classList.toggle("active", state.recruitmentTierFilter === tier);
    button.append(makeElement("b", "", tier), makeElement("small", "", `${label} · ${count}`));
    button.addEventListener("click", () => {
      state.recruitmentTierFilter = tier;
      renderRecruitmentJobs(jobs);
    });
    tierSummary.appendChild(button);
  });
  elements.recruitmentJobs.appendChild(tierSummary);
  elements.recruitmentJobs.appendChild(
    makeElement("p", "job-tier-legend", "分层规则：T0 ≥90 · T0.5 85–89 · T1 80–84 · T1.5 75–79 · T2 70–74 · T2.5 65–69 · T3 60–64；低于 60 分不进入重点池。"),
  );
  const displayedJobs = state.recruitmentTierFilter === "ALL"
    ? availableJobs
    : availableJobs.filter((job) => job.tier_code === state.recruitmentTierFilter);
  elements.recruitmentJobs.appendChild(
    makeElement(
      "p",
      "job-tier-filter-result",
      `当前显示 ${displayedJobs.length} / ${availableJobs.length} 个机会${state.recruitmentTierFilter === "ALL" ? "" : ` · ${state.recruitmentTierFilter}`}`,
    ),
  );
  if (!displayedJobs.length) {
    elements.recruitmentJobs.appendChild(
      makeElement("div", "empty-list", `当前没有 ${state.recruitmentTierFilter} 机会；可以切换其他层级或选择“全部”。`),
    );
    return;
  }
  tierOrder.forEach((tier) => {
    const tierJobs = displayedJobs.filter((job) => (job.tier_code || "T3") === tier);
    if (!tierJobs.length) return;
    const group = makeElement("section", "recruitment-tier-group");
    const heading = makeElement("div", "recruitment-tier-heading");
    heading.append(makeElement("strong", `job-tier ${tier.replace(".", "-")}`, tier), makeElement("span", "", `${tierJobs.length} 个匹配信号`));
    group.appendChild(heading);
    tierJobs.forEach((job) => {
    const card = document.createElement("article");
    card.className = "recruitment-job-card";
    const deadline = job.days_left == null ? "截止日期待官方确认" : (job.days_left === 0 ? "今天截止" : `${job.days_left} 天后截止`);
    const tierCode = tierOrder.includes(job.tier_code) ? job.tier_code : "T3";
    const top = makeElement("div", "job-card-top");
    const labels = document.createElement("div");
    labels.append(
      makeElement("span", "job-company", job.company || "机会发布方"),
      makeElement("span", "job-type", job.employer_type || "重点雇主"),
    );
    if ((job.tags || []).includes("待官方核验")) {
      labels.append(makeElement("span", "job-verification", "待官方核验"));
    }
    const rank = makeElement("div", "job-rank");
    rank.append(
      makeElement("span", "job-score", `${Number(job.match_score || 0)} 分`),
      makeElement("span", `job-tier ${tierCode.replace(".", "-")}`, tierCode),
    );
    top.append(labels, rank);
    const bottom = makeElement("div", "job-card-bottom");
    const officialLink = makeElement("a", "", "打开原始公告 ↗");
    officialLink.href = job.url;
    officialLink.target = "_blank";
    officialLink.rel = "noreferrer";
    bottom.appendChild(officialLink);
    const reason = document.createElement("details");
    reason.className = "job-tier-reason";
    reason.appendChild(makeElement("summary", "", "为什么是这个级别"));
    const reasonBody = makeElement("div", "job-tier-reason-body");
    const positiveList = makeElement("ul", "positive-reasons");
    (job.positive_reasons || []).slice(0, 3).forEach((item) => positiveList.appendChild(makeElement("li", "", item)));
    const negativeList = makeElement("ul", "negative-reasons");
    (job.negative_reasons || []).slice(0, 2).forEach((item) => negativeList.appendChild(makeElement("li", "", item)));
    const flags = (job.fit_tags || []).length
      ? `适配标签：${job.fit_tags.join(" · ")}`
      : "适配标签：等待更多官方岗位信息";
    reasonBody.append(
      makeElement("strong", "", `综合得分 ${Number(job.match_score || 0)} / 100`),
      makeElement("span", "", "主要加分"),
      positiveList,
      makeElement("span", "", "主要减分 / 待核对"),
      negativeList,
      makeElement("small", "", flags),
    );
    reason.appendChild(reasonBody);
    card.append(
      top,
      makeElement("h4", "", job.title || "机会信号"),
      makeElement("p", "job-meta", `${job.city || "地点待确认"} · ${job.industry || "行业待确认"} · ${deadline}`),
      makeElement("p", "job-requirements", job.requirements || "请打开官方公告核对申请条件。"),
      reason,
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
        : "已开始追踪这条原始公告；页面变化会在首页待核对区提示。",
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
    renderRecruitmentDeadlineAlerts(filterRecruitmentByStarfield(state.recruitmentJobs));
    renderHomeRecruitmentAlerts(state.recruitmentJobs, state.recruitmentWatches);
    renderRecruitmentMonitors(data.monitor_pools || []);
    renderRecruitmentSyncStatus(chatgptSyncFromJobs(data));
    setRecruitmentStatus(data.data_status?.message || "已接收公开信号源");
    return data;
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    setRecruitmentStatus("公开信号源加载失败");
    renderRecruitmentSyncStatus(state.recruitmentSyncStatus);
    return null;
  }
}

async function refreshRecruitmentSource() {
  const idleLabel = RECRUITMENT_REFRESH_LABEL;
  elements.recruitmentRefresh.disabled = true;
  elements.recruitmentRefresh.classList.add("is-syncing");
  elements.recruitmentRefresh.setAttribute("aria-busy", "true");
  elements.recruitmentRefresh.textContent = "公开源 + AI 补漏同步中…";
  elements.recruitmentError.textContent = "";
  setRecruitmentStatus("正在同步公开来源、官网哨站与低频 AI 补漏；AI 有 15 分钟冷却并会产生少量 Token 消耗…");
  try {
    const results = await Promise.allSettled([
      api("/recruitment/refresh?deep_search=true", { method: "POST", timeoutMs: 90000 }),
      api("/recruitment/watches/refresh", { method: "POST", timeoutMs: 90000 }),
    ]);
    if (results.every((result) => result.status === "rejected")) throw results[0].reason;
    const refreshedData = await refreshRecruitment();
    const jobsOk = results[0].status === "fulfilled";
    const watchesOk = results[1].status === "fulfilled";
    const sourceResult = jobsOk ? results[0].value : null;
    const sourceCopy = sourceResult
      ? sourceResult.cached
        ? "公开源：沿用 60 秒内的缓存结果"
        : `公开源：返回 ${Number(sourceResult.count || 0).toLocaleString()} 条候选（非新增数）`
      : "公开源：本次未完成";
    const webSearchCopy = sourceResult
      ? formatDeepSearchOutcome(sourceResult)
      : "AI 补漏：本次未完成";
    const watchResult = watchesOk ? results[1].value : null;
    const checkedWatches = Number(watchResult?.checked ?? watchResult?.count ?? watchResult?.refreshed);
    const watchCopy = watchesOk
      ? (Number.isFinite(checkedWatches) ? `官网哨站：本次核对 ${checkedWatches.toLocaleString("zh-CN")} 个` : "官网哨站：本次已刷新")
      : "官网哨站：本次未完成";
    const bridgeCopy = refreshedData
      ? "已读取受控桥当前状态，未将历史 AI 结果计作本轮"
      : "受控桥状态本次未能重新读取，未将历史 AI 结果计作本轮";
    showToast(
      `${sourceCopy}；${watchCopy}；${webSearchCopy}。${bridgeCopy}。`,
      7000,
    );
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    setRecruitmentStatus("公开来源同步未完成；当前列表保持不变");
    showToast("公开源或官网哨站同步未完成；当前岗位列表没有被清空，请稍后重试。", 5500);
  } finally {
    elements.recruitmentRefresh.disabled = false;
    elements.recruitmentRefresh.classList.remove("is-syncing");
    elements.recruitmentRefresh.removeAttribute("aria-busy");
    elements.recruitmentRefresh.textContent = idleLabel;
  }
}

async function openRecruitment() {
  elements.recruitmentError.textContent = "";
  if (!elements.recruitmentDialog.open) {
    elements.recruitmentDialog.showModal();
    playSceneEntry(elements.recruitmentDialog);
  }
  await refreshRecruitment();
}

let recruitmentAutoFilterTimer = null;

async function saveRecruitment(event, { silent = false } = {}) {
  event?.preventDefault?.();
  window.clearTimeout(recruitmentAutoFilterTimer);
  const saveButton = elements.recruitmentSave;
  const saveLabel = saveButton?.querySelector("span");
  if (saveButton && !silent) saveButton.disabled = true;
  if (saveLabel && !silent) saveLabel.textContent = "匹配中…";
  elements.recruitmentError.textContent = "";
  setRecruitmentStatus("正在校准坐标并匹配机会信号…");
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
    renderRecruitmentDeadlineAlerts(filterRecruitmentByStarfield(state.recruitmentJobs));
    renderHomeRecruitmentAlerts(state.recruitmentJobs, state.recruitmentWatches);
    renderRecruitmentMonitors(data.monitor_pools || []);
    setRecruitmentStatus(data.data_status?.message || "已接收公开信号源");
    if (!silent) showToast("坐标已保存，机会匹配已更新。", 3500);
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    setRecruitmentStatus("保存未完成，可稍后重试");
  } finally {
    if (saveButton && !silent) saveButton.disabled = false;
    if (saveLabel && !silent) saveLabel.textContent = "保存坐标并重新扫描";
  }
}

function scheduleRecruitmentAutoFilter() {
  window.clearTimeout(recruitmentAutoFilterTimer);
  recruitmentAutoFilterTimer = window.setTimeout(() => {
    saveRecruitment(null, { silent: true });
  }, 220);
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
    window.setTimeout(openWorldMap, 120);
  } catch (error) { showToast(translateError(error.message), 5000); }
}

function updateNetwork() {
  const online = navigator.onLine;
  elements.networkStatus.classList.toggle("offline", !online);
  elements.networkStatus.querySelector("b").textContent = online ? "在线" : "离线";
}

let adminUsageToken = "";
let adminUsageTimer = null;
let adminUsageLoading = false;

function metricAt(source, paths, fallback = 0) {
  for (const path of paths) {
    const value = path.split(".").reduce((current, key) => current?.[key], source);
    if (value !== undefined && value !== null && value !== "") return value;
  }
  return fallback;
}

function formatUsageNumber(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value ?? 0);
  return new Intl.NumberFormat("zh-CN", { notation: numeric >= 100000 ? "compact" : "standard", maximumFractionDigits: 1 }).format(numeric);
}

function renderAdminUsage(data) {
  const cards = [
    ["账号总数", ["totals.users", "totals.total_users", "total_users", "users"]],
    ["当前活跃 · 15m", ["live.active_users", "live.users"]],
    ["24h 活跃", ["totals.active_users_24h", "recent.active_users_24h", "active_users_24h"]],
    ["会话总数", ["totals.sessions", "totals.total_sessions", "total_sessions", "sessions"]],
    ["消息总数", ["totals.messages", "totals.total_messages", "total_messages", "messages"]],
    ["文档总数", ["totals.documents", "totals.total_documents", "total_documents", "documents"]],
    ["API 请求 · 15m", ["live.api_requests", "live.requests"]],
    ["24h 已记录调用", ["recent.ai_requests_24h", "totals.ai_requests_24h", "ai_requests_24h"]],
    ["已记录输入 Token", ["totals.input_tokens", "totals.prompt_tokens", "input_tokens", "prompt_tokens"]],
    ["已记录输出 Token", ["totals.output_tokens", "totals.completion_tokens", "output_tokens", "completion_tokens"]],
    ["API 错误", ["totals.api_errors", "errors.api_errors"]],
    ["服务端错误", ["totals.server_errors", "errors.server_errors"]],
  ];
  elements.adminUsageCards.replaceChildren();
  cards.forEach(([label, paths]) => {
    const article = document.createElement("article");
    const small = document.createElement("small");
    const strong = document.createElement("strong");
    small.textContent = label;
    strong.textContent = formatUsageNumber(metricAt(data, paths));
    article.append(small, strong);
    elements.adminUsageCards.appendChild(article);
  });

  const series = data.series || data.daily || data.trend || [];
  elements.adminUsageSeries.replaceChildren();
  if (!Array.isArray(series) || !series.length) {
    const empty = document.createElement("p");
    empty.textContent = "暂无趋势数据；汇总计数仍会每 10 秒更新。";
    elements.adminUsageSeries.appendChild(empty);
  } else {
    series.slice(-14).reverse().forEach((point) => {
      const row = document.createElement("div");
      const date = document.createElement("time");
      const active = document.createElement("span");
      const messages = document.createElement("span");
      const requests = document.createElement("span");
      const tokens = document.createElement("span");
      date.textContent = point.date || point.day || point.period || "—";
      active.textContent = `活跃 ${formatUsageNumber(point.active_users ?? point.active ?? 0)}`;
      messages.textContent = `消息 ${formatUsageNumber(point.messages ?? 0)}`;
      requests.textContent = `调用 ${formatUsageNumber(point.ai_requests ?? point.requests ?? 0)}`;
      tokens.textContent = `Token ${formatUsageNumber(point.tokens ?? ((point.input_tokens || 0) + (point.output_tokens || 0)))}`;
      row.append(date, active, messages, requests, tokens);
      elements.adminUsageSeries.appendChild(row);
    });
  }

  const generatedAt = data.generated_at || data.updated_at || new Date().toISOString();
  const parsed = new Date(generatedAt);
  elements.adminUsageUpdated.textContent = Number.isNaN(parsed.getTime()) ? `更新于 ${generatedAt}` : `更新于 ${parsed.toLocaleString("zh-CN")}`;
}

async function refreshAdminUsage() {
  if (!adminUsageToken || adminUsageLoading) return false;
  adminUsageLoading = true;
  elements.adminUsageStatus.textContent = "正在同步汇总数据…";
  elements.adminUsageError.textContent = "";
  elements.adminUsageRefresh.disabled = true;
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(`${API_BASE}/admin/usage`, {
      headers: { "X-Admin-Token": adminUsageToken },
      signal: controller.signal,
    });
    if (!response.ok) {
      if (response.status === 401 || response.status === 403) throw new Error("管理员 Token 不正确或已失效。");
      throw new Error(`使用数据暂时不可用（HTTP ${response.status}）。`);
    }
    renderAdminUsage(await response.json());
    elements.adminUsageStatus.textContent = "实时连接 · 每 10 秒自动刷新";
    return true;
  } catch (error) {
    elements.adminUsageStatus.textContent = "连接中断";
    elements.adminUsageError.textContent = error.name === "AbortError" ? "同步超时，请稍后重试。" : error.message;
    return false;
  } finally {
    window.clearTimeout(timeout);
    adminUsageLoading = false;
    elements.adminUsageRefresh.disabled = false;
  }
}

function stopAdminUsagePolling() {
  window.clearInterval(adminUsageTimer);
  adminUsageTimer = null;
}

function startAdminUsagePolling(refreshNow = true) {
  stopAdminUsagePolling();
  if (!adminUsageToken) return;
  if (refreshNow) refreshAdminUsage();
  adminUsageTimer = window.setInterval(() => {
    if (!document.hidden && elements.adminUsageDialog.open) refreshAdminUsage();
  }, 10000);
}

function lockAdminUsage() {
  stopAdminUsagePolling();
  adminUsageToken = "";
  elements.adminUsageToken.value = "";
  elements.adminUsageContent.classList.add("hidden");
  elements.adminUsageAuth.classList.remove("hidden");
  elements.adminUsageCards.replaceChildren();
  elements.adminUsageSeries.replaceChildren();
  elements.adminUsageError.textContent = "";
  elements.adminUsageStatus.textContent = "每 10 秒自动刷新";
}

function openAdminUsage() {
  if (!elements.adminUsageDialog.open) elements.adminUsageDialog.showModal();
  if (adminUsageToken) {
    elements.adminUsageAuth.classList.add("hidden");
    elements.adminUsageContent.classList.remove("hidden");
    startAdminUsagePolling();
  } else {
    window.setTimeout(() => elements.adminUsageToken.focus(), 80);
  }
}

function openRegistrationFromLink() {
  setAuthMode("register");
  window.setTimeout(() => {
    document.querySelector(".auth-card")?.scrollIntoView({
      behavior: "auto",
      block: "center",
    });
  }, 120);
}

elements.authForm.addEventListener("submit", authenticate);
elements.authSwitch.addEventListener("click", () => setAuthMode(state.authMode === "login" ? "register" : "login"));
elements.authModeLogin.addEventListener("click", () => setAuthMode("login"));
elements.authModeRegister.addEventListener("click", () => setAuthMode("register"));
$("new-chat").addEventListener("click", newConversation);
$("brand-home").addEventListener("click", (event) => { event.preventDefault(); openWorldMap(); });
$("world-map-open").addEventListener("click", openWorldMap);
$("mobile-world-map-open").addEventListener("click", openWorldMap);
$("world-map-close").addEventListener("click", () => elements.worldMapDialog.close());
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
$("music-open").addEventListener("click", openMusicDimension);
$("mobile-music-open").addEventListener("click", openMusicDimension);
$("resonance-open").addEventListener("click", () => openConcept(elements.resonanceDialog));
$("trace-open").addEventListener("click", () => openConcept(elements.traceDialog));
$("mobile-recruitment-open").addEventListener("click", openRecruitment);
$("mobile-trace-open").addEventListener("click", () => openConcept(elements.traceDialog));
$("mobile-resonance-open").addEventListener("click", () => openConcept(elements.resonanceDialog));
$("home-alert-open").addEventListener("click", openRecruitment);
$("recruitment-close").addEventListener("click", () => elements.recruitmentDialog.close());
elements.recruitmentRefresh.addEventListener("click", refreshRecruitmentSource);
elements.recruitmentForm.addEventListener("submit", saveRecruitment);
document.querySelectorAll(".recruitment-checks input").forEach((input) => {
  input.addEventListener("change", () => {
    state.recruitmentTierFilter = "ALL";
    renderRecruitmentJobs(state.recruitmentJobs);
    renderRecruitmentDeadlineAlerts(filterRecruitmentByStarfield(state.recruitmentJobs));
    const selected = selectedRecruitmentStarfields();
    setRecruitmentStatus(selected.length ? `已即时筛选：${selected.join(" · ")}；正在同步保存你的星域坐标…` : "已显示全部信号星域；正在同步保存坐标…");
    scheduleRecruitmentAutoFilter();
  });
});
[elements.recruitmentRoles, elements.recruitmentIndustries, elements.recruitmentLocations].forEach((input) => {
  input.addEventListener("change", scheduleRecruitmentAutoFilter);
});
elements.recruitmentWatchForm.addEventListener("submit", addRecruitmentWatch);
$("studio-open-secondary").addEventListener("click", openStudio);
$("mobile-studio-open").addEventListener("click", openStudio);
$("mobile-more-open").addEventListener("click", openWorldMap);
$("studio-close").addEventListener("click", () => elements.studioDialog.close());
elements.spaceForm.addEventListener("submit", createSpace);
elements.runnerSend.addEventListener("click", runActiveSpace);
elements.runnerInput.addEventListener("input", scheduleSpacePreflight);
document.querySelectorAll('input[name="runner-mode"]').forEach((input) => input.addEventListener("change", updateRunnerModeCopy));
elements.runnerHistoryRefresh.addEventListener("click", loadSpaceHistory);
$("billing-upgrade").addEventListener("click", explainBillingSetup);
$("cross-exam-open").addEventListener("click", openCrossExam);
$("world-map-cross-open").addEventListener("click", () => {
  elements.worldMapDialog.close();
  openCrossExam();
});
$("music-dialog-close").addEventListener("click", closeMusicDimension);
elements.musicDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeMusicDimension();
});
elements.musicDialogMinimize.addEventListener("click", minimizeMusicDimension);
elements.musicFooterMinimize.addEventListener("click", minimizeMusicDimension);
$("music-mini-open").addEventListener("click", openMusicDimension);
elements.musicEnable.addEventListener("click", generateLocalSoundscape);
elements.musicDisable.addEventListener("click", disableMusicDimension);
elements.musicRewrite.addEventListener("click", async () => {
  rewriteMusicBlueprint();
  await persistMusicPreferences();
  showToast("创作描述已在本地重写。", 2200);
});
elements.musicCopyBlueprint.addEventListener("click", copyMusicBlueprint);
elements.musicCreationTempo.addEventListener("input", () => {
  elements.musicTempoOutput.textContent = `${elements.musicCreationTempo.value} BPM`;
});
elements.musicVocalProfile.addEventListener("change", async () => {
  state.music.vocalProfile = elements.musicVocalProfile.value;
  rewriteMusicBlueprint();
  await persistMusicPreferences();
});
document.querySelectorAll("[data-music-instrument]").forEach((input) => {
  input.addEventListener("change", async () => {
    const selected = [...document.querySelectorAll("[data-music-instrument]:checked")];
    if (!selected.length) {
      input.checked = true;
      showToast("至少保留一种乐器。", 2200);
      return;
    }
    state.music.creationInstruments = selected.map((item) => item.dataset.musicInstrument);
    rewriteMusicBlueprint();
    await persistMusicPreferences();
  });
});
document.querySelectorAll("[data-music-template]").forEach((button) => {
  button.addEventListener("click", async () => {
    applyMusicCreationTemplate(button.dataset.musicTemplate);
    await persistMusicPreferences();
  });
});
elements.musicPlayToggle.addEventListener("click", toggleMusicPlayback);
elements.musicMiniToggle.addEventListener("click", toggleMusicPlayback);
elements.musicMiniMute.addEventListener("click", toggleMusicMute);
elements.musicVolume.addEventListener("input", () => {
  elements.musicVolumeOutput.textContent = `${Math.round(Number(elements.musicVolume.value) * 100)}%`;
});
elements.musicVolume.addEventListener("change", () => changeMusicVolume(elements.musicVolume.value));
$("photon-projection-close").addEventListener("click", () => elements.photonDialog.close());
elements.photonDialog.addEventListener("cancel", (event) => {
  if (state.photon.loading) event.preventDefault();
});
elements.photonInspiration.addEventListener("input", () => {
  elements.photonInputCount.textContent = String(elements.photonInspiration.value.length);
});
document.querySelectorAll('input[name="photon-source"]').forEach((input) => {
  input.addEventListener("change", updatePhotonSourceMode);
});
document.querySelectorAll("[data-photon-style]").forEach((input) => {
  input.addEventListener("input", () => {
    const output = $(`photon-${input.dataset.photonStyle}-output`);
    if (output) output.textContent = input.value;
  });
});
elements.photonSkeleton.addEventListener("click", generatePhotonSkeleton);
elements.photonProject.addEventListener("click", startPhotonProjection);
elements.photonCopy.addEventListener("click", copyPhotonResult);
elements.photonSave.addEventListener("click", savePhotonCreation);
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
document.querySelectorAll("[data-mobile-workspace]").forEach((button) => {
  button.addEventListener("click", () => changeWorkspace(button.dataset.mobileWorkspace));
});
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
document.querySelectorAll("[data-close-concept]").forEach((button) => {
  button.addEventListener("click", () => closeConcept(button.dataset.closeConcept));
});
window.addEventListener("online", updateNetwork);
window.addEventListener("offline", updateNetwork);
window.addEventListener("resize", () => { if (window.innerWidth > 1180) closePanels(); });

elements.adminUsageLauncher.addEventListener("click", openAdminUsage);
elements.adminUsageClose.addEventListener("click", () => elements.adminUsageDialog.close());
elements.adminUsageDialog.addEventListener("close", stopAdminUsagePolling);
elements.adminUsageAuth.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = elements.adminUsageToken.value.trim();
  if (!token) return;
  adminUsageToken = token;
  elements.adminUsageConnect.disabled = true;
  elements.adminUsageConnect.querySelector("span").textContent = "正在验证…";
  const connected = await refreshAdminUsage();
  elements.adminUsageConnect.disabled = false;
  elements.adminUsageConnect.querySelector("span").textContent = "连接实时数据";
  if (!connected) {
    adminUsageToken = "";
    return;
  }
  elements.adminUsageToken.value = "";
  elements.adminUsageAuth.classList.add("hidden");
  elements.adminUsageContent.classList.remove("hidden");
  startAdminUsagePolling(false);
});
elements.adminUsageRefresh.addEventListener("click", refreshAdminUsage);
elements.adminUsageLock.addEventListener("click", lockAdminUsage);

initOblivionArchive({ notify: showToast });
setupRotaryCompasses();

(async function bootstrap() {
  updateNetwork();
  await loadMusicPreferences();
  const initialParams = new URLSearchParams(window.location.search);
  if (initialParams.get("admin") === "usage") {
    elements.adminUsageLauncher.classList.remove("hidden");
    window.setTimeout(openAdminUsage, 80);
  }
  if (initialParams.get("start") === "register") openRegistrationFromLink();
  state.token = await storage.get(STORAGE_KEYS.token);
  state.workspace = (await storage.get(STORAGE_KEYS.workspace)) || "general";
  state.activeProduct = await storage.get(STORAGE_KEYS.activeProduct);
  if (Capacitor.isNativePlatform() && !configuredApiBase) {
    elements.authError.textContent = "移动端构建尚未配置正式 HTTPS API 地址。";
  }
  if (!state.token) {
    if (["oblivion", "resonance", "trace"].includes(state.activeProduct)) {
      window.setTimeout(() => launchProduct(state.activeProduct), 80);
    }
    return;
  }
  try {
    state.user = await api("/auth/me");
    await enterApp();
  } catch (_) { await logout(false); }
})();
