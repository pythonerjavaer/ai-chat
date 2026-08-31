import DOMPurify from "dompurify";
import { marked } from "marked";
import { Capacitor } from "@capacitor/core";
import { Haptics, ImpactStyle } from "@capacitor/haptics";
import { Preferences } from "@capacitor/preferences";
import { MUSIC_CREATION_TEMPLATES, buildMusicBlueprint, soundscapeEngine } from "./music-creator.js";
import { initOblivionArchive, openOblivionArchive } from "./oblivion-archive.js";
import {
  PRODUCT_NAV_ITEMS,
  normalizeProductId,
  productDialogIdsToClose,
  resolveStartupProduct,
} from "./product-navigation.js";
import {
  DEFAULT_FUTURE_RADAR_STATUS,
  FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
  TIER_CODES,
  buildFutureRadarJobsQuery,
  buildFutureRadarCompanyJobsQuery,
  canonicalStarfieldCode,
  createCoalescedRadarReload,
  filterJobsByStarfields,
  formatOrganizationAssessment,
  formatScoringFactors,
  futureRadarActiveRunTypes,
  futureRadarAiSearchNotice,
  futureRadarCoverageCopy,
  futureRadarOpportunityDateCopy,
  futureRadarOpportunityErrorCopy,
  futureRadarOpportunitySource,
  futureRadarPublicOpportunityUrl,
  futureRadarRunErrorCopy,
  futureRadarRunSuccessCopy,
  futureRadarSourceErrorCopy,
  jobTierBucket,
  partitionJobsByPriority,
  starfieldLabel,
} from "./recruitment-radar.js";
import "./styles.css";
import { radarPollingGate, RADAR_STATUS_INTERVAL_MS } from "./radar-polling.js";

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
  pendingProduct: "bingyan_pending_product",
  photonCreations: "bingyan_photon_creations",
};
const WORKSPACE_ORDER = ["legal", "general", "finance"];
const CHATGPT_MONITOR_SOURCE_COUNT = 6;
const RECRUITMENT_REFRESH_LABEL = "同步候选源 ↻";
const FUTURE_RADAR_POLL_INTERVAL_MS = 30_000;
const FUTURE_RADAR_RUN_STATUS_POLL_MS = RADAR_STATUS_INTERVAL_MS;
const FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS = 20;
const FUTURE_RADAR_SCAN_TYPES = Object.freeze(["quick", "deep"]);
const FUTURE_RADAR_REQUEST_CONTROLLERS = new Set();
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
  futureRadar: {
    dashboard: null,
    view: "companies",
    companies: [],
    totalCompanies: 0,
    deadlineJobs: [],
    companyExpansions: new Map(),
    jobs: [],
    jobsLoaded: false,
    jobsError: "",
    totalJobs: 0,
    page: 1,
    pageSize: 20,
    opportunityStats: {},
    jobsRequestId: 0,
    jobsLoading: false,
    jobsRequestQuery: "",
    jobsRequestController: null,
    jobsRequestPromise: null,
    jobsAppliedQuery: "",
    jobsAppliedTier: "ALL",
    jobsAppliedView: "companies",
    jobsAppliedPage: 1,
    jobsAppliedPageSize: 20,
    pollOpportunityController: null,
    snapshotRequestId: 0,
    searchScope: {},
    searchCoverage: null,
    searchStatus: "pending",
    programs: [],
    events: [],
    sources: [],
    runs: [],
    activeTab: "jobs",
    lastEventId: null,
    pollingTimer: null,
    polling: false,
    loading: false,
    runStarting: { quick: false, deep: false },
    runDelayUntil: { quick: 0, deep: 0 },
    runDelayTimer: { quick: null, deep: null },
    runStatusPollTimer: { quick: null, deep: null },
    runStatusPollPending: { quick: false, deep: false },
    runStatusTracking: { quick: false, deep: false },
    terminalSnapshotPromise: null,
    activeRunTypes: new Set(),
    filters: { q: "", company: "", city: "", industry: "", employer_type: "", program_id: "", status: DEFAULT_FUTURE_RADAR_STATUS, verification_status: "", source_id: "", event_type: "", sort: "changed", opening_after: "", opening_before: "", closing_after: "", closing_before: "" },
  },
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

let productLaunchReady = false;
let queuedProductLaunch = null;

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
  futureRadarRun: $("future-radar-run"), futureRadarRunLabel: $("future-radar-run-label"),
  futureRadarDeepRun: $("future-radar-deep-run"), futureRadarDeepRunLabel: $("future-radar-deep-run-label"),
  futureRadarActionStatus: $("future-radar-action-status"), futureRadarDashboard: $("future-radar-dashboard"),
  futureRadarLastScan: $("future-radar-last-scan"), futureRadarLastSuccess: $("future-radar-last-success"),
  futureRadarSourceHealth: $("future-radar-source-health"), futureRadarLiveState: $("future-radar-live-state"),
  futureRadarLoading: $("future-radar-loading"), futureRadarError: $("future-radar-error"),
  futureRadarPrograms: $("future-radar-programs"), futureRadarEvents: $("future-radar-events"),
  futureRadarSources: $("future-radar-sources"), futureRadarRuns: $("future-radar-runs"),
  futureRadarPagination: $("future-radar-pagination"), futureRadarPagePrev: $("future-radar-page-prev"),
  futureRadarPageNext: $("future-radar-page-next"), futureRadarPageStatus: $("future-radar-page-status"),
  futureRadarOpportunityCount: $("future-radar-opportunity-count"),
  futureRadarOpportunityRefresh: $("future-radar-opportunity-refresh"),
  futureRadarOpportunitySummary: $("future-radar-opportunity-summary"),
  futureRadarOpportunityCoverage: $("future-radar-opportunity-coverage"),
  futureRadarFilterForm: $("future-radar-filter-form"), futureRadarFilterQuery: $("future-radar-filter-query"),
  futureRadarFilterCompany: $("future-radar-filter-company"), futureRadarFilterCity: $("future-radar-filter-city"),
  futureRadarFilterIndustry: $("future-radar-filter-industry"), futureRadarFilterEmployerType: $("future-radar-filter-employer-type"),
  futureRadarFilterProgram: $("future-radar-filter-program"),
  futureRadarFilterStatus: $("future-radar-filter-status"), futureRadarFilterVerification: $("future-radar-filter-verification"),
  futureRadarFilterSource: $("future-radar-filter-source"), futureRadarFilterEvent: $("future-radar-filter-event"),
  futureRadarFilterSort: $("future-radar-filter-sort"), futureRadarFilterOpeningAfter: $("future-radar-filter-opening-after"),
  futureRadarFilterOpeningBefore: $("future-radar-filter-opening-before"), futureRadarFilterClosingAfter: $("future-radar-filter-closing-after"),
  futureRadarFilterClosingBefore: $("future-radar-filter-closing-before"), futureRadarFilterReset: $("future-radar-filter-reset"),
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
  elements.recruitmentRefresh.title = "同步已有候选源与官网哨站；如需寻找新入口，请单独启动 Deep Scan。";
  elements.recruitmentRefresh.setAttribute("aria-label", "同步已有候选源与官网哨站");
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
const productSwitchers = new Set();

function updateProductSwitchers(product) {
  const activeProduct = normalizeProductId(product);
  productSwitchers.forEach((navigation) => {
    navigation.querySelectorAll("[data-product-switch]").forEach((button) => {
      const active = button.dataset.productSwitch === activeProduct;
      button.classList.toggle("active", active);
      if (active) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
  });
}

function createProductSwitcher(context) {
  const navigation = document.createElement("nav");
  navigation.className = "product-switcher";
  navigation.dataset.productSwitcher = context;
  navigation.setAttribute("aria-label", "切换冰焰产品");

  const worldMapButton = document.createElement("button");
  worldMapButton.type = "button";
  worldMapButton.className = "product-switcher-map";
  worldMapButton.dataset.productMapOpen = "true";
  worldMapButton.title = "冰焰世界地图";
  worldMapButton.setAttribute("aria-label", "打开冰焰世界地图");
  worldMapButton.append(makeElement("i", "", "◈"), makeElement("span", "", "世界地图"));
  worldMapButton.addEventListener("click", openWorldMap);
  navigation.appendChild(worldMapButton);

  PRODUCT_NAV_ITEMS.forEach((item) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.productSwitch = item.id;
    button.title = `${item.label} · ${item.english}`;
    button.setAttribute("aria-label", `切换到${item.label}`);
    button.append(makeElement("i", "", item.symbol), makeElement("span", "", item.label));
    button.addEventListener("click", () => launchProduct(item.id));
    navigation.appendChild(button);
  });
  productSwitchers.add(navigation);
  return navigation;
}

function setupProductSwitchers() {
  const appHeader = elements.appView.querySelector(":scope > .app-header");
  if (appHeader && !elements.appView.querySelector(":scope > [data-product-switcher]")) {
    appHeader.after(createProductSwitcher("workspace"));
  }

  PRODUCT_NAV_ITEMS.filter((item) => item.dialogId).forEach((item) => {
    const dialog = $(item.dialogId);
    const shell = dialog?.firstElementChild;
    const header = shell ? [...shell.children].find((child) => child.matches("header")) : null;
    if (!header || shell.querySelector(":scope > [data-product-switcher]")) return;
    header.after(createProductSwitcher(item.id));
  });
  updateProductSwitchers(state.activeProduct || state.workspace);
}

function closeOpenProductDialogs(nextProduct = null) {
  productDialogIdsToClose(nextProduct).forEach((dialogId) => {
    const dialog = $(dialogId);
    if (!dialog?.open) return;
    dialog.close();
    if (dialogId === "music-dimension-dialog") {
      state.music.minimized = state.music.enabled;
      renderMusicUI();
    }
  });
}

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
    pointerId: null,
    pressedCard: null,
    pointerActivation: null,
    suppressNextClick: false,
    selectedIndex: 0,
    wheelLocked: false,
  };
  const step = () => (Math.PI * 2) / compass.cards.length;
  const render = () => {
    const compact = window.innerWidth <= 520;
    const radiusX = compact ? Math.min(240, container.clientWidth * .78) : Math.min(id === "landing" ? 320 : 300, container.clientWidth * .39);
    const baseRadiusY = compact ? 118 : id === "landing" ? 104 : 94;
    let selectedIndex = 0;
    let selectedDepth = -1;
    const layout = compass.cards.map((card, index) => {
      const angle = compass.rotation + index * step();
      const cosine = Math.cos(angle);
      const depth = (cosine + 1) / 2;
      const scale = .54 + depth * .46;
      const x = Math.sin(angle) * radiusX;
      card.dataset.compassDepth = depth.toFixed(3);
      if (depth > selectedDepth) {
        selectedDepth = depth;
        selectedIndex = index;
      }
      return { card, index, cosine, depth, scale, x };
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
    // Opposite sides of the ellipse can have the same x coordinate. Size the
    // vertical orbit from actual cards so a foreground card cannot cover the
    // centre of another visible button (notably EMBER over Future Radar on
    // the initial map). Keep that radius stable throughout a drag.
    const inDrag = compass.dragging && compass.moved;
    let radiusY = inDrag ? compass.radiusY || baseRadiusY : baseRadiusY;
    if (!inDrag) {
      const measured = layout.map((item) => ({ ...item, width: item.card.offsetWidth, height: item.card.offsetHeight }));
      for (const target of measured) {
        if (target.depth < .08) continue;
        for (const foreground of measured) {
          if (foreground.depth <= target.depth || !foreground.width || !foreground.height) continue;
          const horizontalGap = Math.abs(foreground.x - target.x);
          const verticalFactor = Math.abs(foreground.cosine - target.cosine);
          if (horizontalGap < foreground.width * foreground.scale / 2 + 8 && verticalFactor > .001) {
            radiusY = Math.max(radiusY, (foreground.height * foreground.scale / 2 + 8) / verticalFactor);
          }
        }
      }
      compass.radiusY = radiusY;
    }
    layout.forEach(({ card, depth, scale, x, cosine }) => {
      card.style.setProperty("--compass-x", `${x}px`);
      card.style.setProperty("--compass-y", `${cosine * radiusY}px`);
      card.style.setProperty("--compass-scale", scale.toFixed(3));
      card.style.setProperty("--compass-opacity", (.12 + depth * .88).toFixed(3));
      card.style.setProperty("--compass-blur", `${((1 - depth) * 2.6).toFixed(2)}px`);
      card.style.zIndex = String(Math.round(10 + depth * 90));
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
  const rotateCardToFront = (card) => {
    const index = compass.cards.indexOf(card);
    if (index < 0 || index === compass.selectedIndex) return;
    const baseRotation = -index * step();
    const fullTurn = Math.PI * 2;
    compass.rotation = baseRotation + Math.round((compass.rotation - baseRotation) / fullTurn) * fullTurn;
    snap();
  };
  const visibleCard = (target) => {
    const card = target?.closest?.('[data-launch][aria-hidden="false"]');
    return card && cards.includes(card) ? card : null;
  };
  container.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.isPrimary === false || compass.dragging) return;
    compass.dragging = true;
    compass.moved = false;
    compass.pointerId = event.pointerId;
    compass.pressedCard = visibleCard(event.target);
    compass.pointerActivation = null;
    compass.suppressNextClick = false;
    compass.startX = event.clientX;
    compass.startY = event.clientY;
    compass.startRotation = compass.rotation;
  });
  container.addEventListener("pointermove", (event) => {
    if (!compass.dragging || event.pointerId !== compass.pointerId) return;
    const deltaX = event.clientX - compass.startX;
    const deltaY = event.clientY - compass.startY;
    if (!compass.moved) {
      // A click (including normal hand jitter) must not move its hit target
      // between pointerdown and click. Only an actual drag changes the orbit.
      if (Math.hypot(deltaX, deltaY) <= 5) return;
      compass.moved = true;
      container.classList.add("is-dragging");
      if (!container.hasPointerCapture(event.pointerId)) container.setPointerCapture(event.pointerId);
    }
    if (event.cancelable && Math.abs(deltaX) >= Math.abs(deltaY)) event.preventDefault();
    compass.rotation = compass.startRotation + deltaX / Math.max(150, container.clientWidth) * Math.PI * 2;
    render();
  });
  const endDrag = (event) => {
    if (!compass.dragging || event.pointerId !== compass.pointerId) return;
    compass.dragging = false;
    container.classList.remove("is-dragging");
    const cancelled = event.type === "pointercancel" || event.type === "lostpointercapture";
    compass.suppressNextClick = compass.moved || cancelled;
    compass.pointerActivation = { card: cancelled || compass.moved ? null : compass.pressedCard };
    compass.pressedCard = null;
    compass.pointerId = null;
    if (container.hasPointerCapture(event.pointerId)) container.releasePointerCapture(event.pointerId);
    // Snapping a non-drag can change which overlapping card receives click.
    if (compass.moved) snap();
  };
  container.addEventListener("pointerup", endDrag);
  container.addEventListener("pointercancel", endDrag);
  container.addEventListener("lostpointercapture", endDrag);
  container.addEventListener("pointerleave", (event) => {
    if (compass.dragging && !compass.moved && event.pointerId === compass.pointerId) {
      endDrag({ ...event, pointerId: event.pointerId, type: "pointercancel" });
    }
  });
  container.addEventListener("click", (event) => {
    // Own compass activation here. The generic [data-launch] listener must
    // not run again after rotation changes the card stacking order.
    event.preventDefault();
    event.stopImmediatePropagation();
    const pointerClick = event.detail !== 0;
    const suppressed = pointerClick && compass.suppressNextClick;
    const card = pointerClick && compass.pointerActivation
      ? compass.pointerActivation.card : visibleCard(event.target);
    compass.pointerActivation = null;
    compass.suppressNextClick = false;
    if (suppressed || !card) return;
    const product = card.dataset.launch;
    rotateCardToFront(card);
    launchProduct(product);
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
  const { preserveAuthOn401 = false, signal: externalSignal, timeoutMs = 15000, ...requestOptions } = options;
  const isRadarRead = (path.startsWith("/future-radar/") || path.startsWith("/recruitment/"))
    && (!options.method || options.method === "GET");
  if (isRadarRead) radarPollingGate.assertAllowed();
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const controller = new AbortController();
  const isRadarRequest = path.startsWith("/future-radar/") || path.startsWith("/recruitment/");
  if (isRadarRequest) FUTURE_RADAR_REQUEST_CONTROLLERS.add(controller);
  const cancel = () => controller.abort(externalSignal.reason);
  externalSignal?.addEventListener("abort", cancel, { once: true });
  if (externalSignal?.aborted) cancel();
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(API_BASE + path, { ...requestOptions, headers, signal: controller.signal });
    if (response.status === 401 && !preserveAuthOn401 && !path.startsWith("/auth/login")) await logout(false);
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { detail = (await response.json()).detail || detail; } catch (_) {}
      const requestError = new Error(detail);
      requestError.status = response.status;
      requestError.retryAfter = response.headers.get("Retry-After");
      throw requestError;
    }
    const result = response.status === 204 ? null : await response.json();
    if (isRadarRead) radarPollingGate.success();
    return result;
  } catch (error) {
    if (controller.signal.aborted && controller.signal.reason?.code === "AUTH_REQUIRED") throw controller.signal.reason;
    // A superseded selection is cancellation, not a network timeout or a reason
    // to replace the latest selection with an error from the previous request.
    if (externalSignal?.aborted) throw externalSignal.reason || error;
    if (isRadarRead) radarPollingGate.failure(error);
    if (error.name === "AbortError") {
      const requestError = new Error("请求超时，请检查服务是否已启动或稍后重试。");
      requestError.code = "REQUEST_TIMEOUT";
      requestError.timeoutMs = timeoutMs;
      throw requestError;
    }
    throw error;
  } finally {
    if (isRadarRequest) FUTURE_RADAR_REQUEST_CONTROLLERS.delete(controller);
    externalSignal?.removeEventListener("abort", cancel);
    clearTimeout(timeout);
  }
}

function showToast(message, timeout = 3600) {
  elements.toast.textContent = message;
  elements.toast.classList.remove("hidden");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => elements.toast.classList.add("hidden"), timeout);
}

function productDisplayName(product) {
  const productButton = document.querySelector(`[data-launch="${product}"]`);
  return productButton?.querySelector("strong")?.textContent?.trim() || "这个世界";
}

function showPendingProductAuth(product) {
  if (!product) return;
  const productName = productDisplayName(product);
  const action = state.authMode === "register" ? "注册后进入" : "登录后进入";
  elements.authKicker.textContent = "世界入口已锁定";
  elements.authTitle.textContent = `${action}${productName}`;
  elements.authDescription.textContent = "完成登录或注册后会自动打开刚才选择的产品，不需要再次寻找入口。";
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
  if (state.pendingLaunch) showPendingProductAuth(state.pendingLaunch);
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
    "Invalid username or password.": "用户名或密码不正确。当前测试环境升级后旧账号可能已失效，可切换到“注册新账号”重新创建。",
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
  const pendingLaunch = state.pendingLaunch || await storage.get(STORAGE_KEYS.pendingProduct);
  const resumeProduct = resolveStartupProduct({ queuedProductLaunch, pendingLaunch });
  queuedProductLaunch = null;
  if (WORKSPACE_ORDER.includes(resumeProduct)) state.workspace = resumeProduct;
  elements.authView.classList.add("hidden");
  elements.appView.classList.remove("hidden");
  applyUser();
  await loadWorkspaces();
  await Promise.all([loadSessions(), loadDocuments(), loadHomeRecruitmentAlerts()]);
  newConversation();
  if (resumeProduct) window.setTimeout(() => launchProduct(resumeProduct), 0);
  if (!state.user.privacy_accepted && !elements.consentDialog.open) {
    elements.consentDialog.showModal();
  } else if (!resumeProduct) {
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
    .map((job) => ({ ...job, days_left: recruitmentDaysLeft(job) }))
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

function endFutureRadarSession(expired = false) {
  radarPollingGate.clearSession();
  stopFutureRadarPolling();
  FUTURE_RADAR_SCAN_TYPES.forEach((scanType) => {
    stopFutureRadarRunStatusPolling(scanType);
    window.clearInterval(state.futureRadar.runDelayTimer[scanType]);
    state.futureRadar.runDelayTimer[scanType] = null;
    state.futureRadar.runDelayUntil[scanType] = 0;
    state.futureRadar.runStarting[scanType] = false;
    if (state.futureRadar.runStatusTracking) state.futureRadar.runStatusTracking[scanType] = false;
  });
  window.clearTimeout(recruitmentAutoFilterTimer);
  recruitmentAutoFilterTimer = null;
  const error = new Error("登录状态已失效，请重新登录。");
  error.status = 401;
  error.code = "AUTH_REQUIRED";
  state.futureRadar.jobsRequestController?.abort(error);
  state.futureRadar.pollOpportunityController?.abort(error);
  FUTURE_RADAR_REQUEST_CONTROLLERS.forEach((controller) => controller.abort(error));
  FUTURE_RADAR_REQUEST_CONTROLLERS.clear();
  state.futureRadar.jobsRequestId += 1;
  state.futureRadar.snapshotRequestId = (state.futureRadar.snapshotRequestId || 0) + 1;
  state.futureRadar.jobsRequestController = null;
  state.futureRadar.jobsRequestPromise = null;
  state.futureRadar.pollOpportunityController = null;
  state.futureRadar.jobsRequestQuery = "";
  state.futureRadar.jobsAppliedQuery = "";
  state.futureRadar.jobsAppliedTier = "ALL";
  state.futureRadar.jobsAppliedView = "companies";
  state.futureRadar.jobsAppliedPage = 1;
  state.futureRadar.jobsAppliedPageSize = 20;
  state.futureRadar.jobsLoading = false;
  state.futureRadar.loading = false;
  state.futureRadar.polling = false;
  state.futureRadar.jobsLoaded = false;
  state.futureRadar.jobs = [];
  state.futureRadar.view = "companies";
  state.futureRadar.page = 1;
  state.futureRadar.pageSize = 20;
  state.futureRadar.companies = [];
  state.futureRadar.deadlineJobs = [];
  state.futureRadar.totalCompanies = 0;
  resetFutureRadarCompanyExpansions();
  state.futureRadar.totalJobs = 0;
  state.futureRadar.opportunityStats = {};
  state.futureRadar.activeRunTypes.clear();
  state.futureRadar.jobsError = expired ? futureRadarOpportunityErrorCopy(error) : "";
  if (elements.recruitmentDialog?.open) elements.recruitmentDialog.close();
}

async function logout(showMessage = true) {
  state.token = null;
  endFutureRadarSession(!showMessage);
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
  updateProductSwitchers(state.workspace);
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
  updateProductSwitchers(workspaceId);
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
  updateProductSwitchers("forge");
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
  updateProductSwitchers(dialog?.dataset.scene);
  closePanels();
  if (!dialog.open) {
    dialog.showModal();
    playSceneEntry(dialog);
  }
}

function openWorldMap() {
  closePanels();
  closeOpenProductDialogs();
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
  updateProductSwitchers("music");
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
  updateProductSwitchers("photon");
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
  product = normalizeProductId(product);
  if (!product) return;
  if (!productLaunchReady) {
    queuedProductLaunch = product;
    return;
  }
  state.activeProduct = product;
  updateProductSwitchers(product);
  await storage.set(STORAGE_KEYS.activeProduct, product);
  closeOpenProductDialogs(product);
  if (elements.worldMapDialog.open) elements.worldMapDialog.close();
  if (product === "resonance") {
    state.pendingLaunch = null;
    await storage.remove(STORAGE_KEYS.pendingProduct);
    return openConcept(elements.resonanceDialog);
  }
  if (product === "trace") {
    state.pendingLaunch = null;
    await storage.remove(STORAGE_KEYS.pendingProduct);
    return openConcept(elements.traceDialog);
  }
  if (product === "oblivion") {
    state.pendingLaunch = null;
    await storage.remove(STORAGE_KEYS.pendingProduct);
    return openOblivionArchive();
  }
  if (!state.token) {
    state.pendingLaunch = product;
    await storage.set(STORAGE_KEYS.pendingProduct, product);
    const productName = productDisplayName(product);
    if (WORKSPACE_ORDER.includes(product)) {
      state.workspace = product;
      await storage.set(STORAGE_KEYS.workspace, product);
    }
    showPendingProductAuth(product);
    document.querySelector(".auth-card")?.scrollIntoView({ behavior: "smooth", block: "center" });
    showToast(`${productName}需要先登录；完成后将自动进入。`, 4200);
    return;
  }
  if (WORKSPACE_ORDER.includes(product)) {
    if (product !== state.workspace) await changeWorkspace(product);
    else playWorkspaceEntry(product);
    if (state.token) {
      state.pendingLaunch = null;
      await storage.remove(STORAGE_KEYS.pendingProduct);
    }
    return;
  }
  if (product === "recruitment") await openRecruitment();
  if (product === "forge") await openStudio();
  if (product === "music") await openMusicDimension();
  if (product === "photon") await openPhotonProjection();
  if (state.token) {
    state.pendingLaunch = null;
    await storage.remove(STORAGE_KEYS.pendingProduct);
  } else {
    state.pendingLaunch = product;
    await storage.set(STORAGE_KEYS.pendingProduct, product);
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

function futureRadarOpportunityReadHint() {
  return `首次读取或扫描更新后可能较慢，本次最多等待 ${Math.ceil(FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS / 1000)} 秒。`;
}

function renderFutureRadarOpportunityStatus() {
  const radar = state.futureRadar;
  const count = (value) => Math.max(0, Number(value) || 0).toLocaleString("zh-CN");
  if (futureRadarSelectionIsPending()) {
    setRecruitmentStatus(`正在读取${futureRadarTierLabel()}的全池筛选结果… 数量将随最新结果一起更新。${futureRadarOpportunityReadHint()}`);
  } else if (radar.jobsLoading) {
    setRecruitmentStatus(radar.jobsLoaded
      ? `正在刷新主机会池；上次成功读取 ${count(radar.totalJobs)} 个机会。${futureRadarOpportunityReadHint()}`
      : `正在读取主机会池（含聊天 / 搜索待核验线索）… ${futureRadarOpportunityReadHint()}`);
  } else if (radar.jobsError) {
    setRecruitmentStatus(radar.jobsError);
  } else if (!radar.jobsLoaded) {
    setRecruitmentStatus("主机会池尚未读取；待核验线索与官网确认机会在同一池展示。");
  } else {
    const counts = radar.opportunityStats?.verification_status || {};
    setRecruitmentStatus(`主机会池 · 当前筛选 ${count(radar.totalJobs)} 个机会${radar.totalCompanies == null ? "" : ` · ${count(radar.totalCompanies)} 个企业分组`} · 官网已确认 ${count(counts.verified)} · 待核验 ${count(counts.pending)} · 信息有差异 ${count(counts.conflicted)}`);
  }
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

function radarCollection(payload, keys = []) {
  if (Array.isArray(payload)) return payload;
  for (const key of ["items", ...keys]) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

function radarNumber(source, paths, fallback = 0) {
  const raw = valueAtPaths(source, paths);
  const value = Number(raw);
  return raw !== null && Number.isFinite(value) ? value : fallback;
}

function formatRadarTime(value, fallback = "—") {
  if (!value) return fallback;
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value))) return String(value);
  return formatSyncTime(value, fallback);
}

function radarStatusCopy(value) {
  const raw = String(value || "unknown").toLowerCase();
  return {
    open: "开放中", closed: "已关闭", reopened: "重新开放", running: "扫描中",
    success: "正常", succeeded: "正常", completed: "已完成", partial_success: "部分完成",
    partial: "部分完成", pending: "待核验", verified: "已核验", conflicted: "存在冲突",
    rejected: "未通过核验", invalid: "无效信号", failed: "失败", error: "异常", skipped: "已跳过", disabled: "已停用", healthy: "健康",
    discovery_limited: "发现受限", access_restricted: "访问受限", unknown: "未知",
  }[raw] || String(value || "未知");
}

function radarStatusClass(value) {
  const raw = String(value || "unknown").toLowerCase();
  if (/verified|success|succeeded|completed|healthy|open|synced|new|discovered|reopened|official_source_found/.test(raw)) return "healthy";
  if (/running|progress|syncing|updated/.test(raw)) return "running";
  if (/error|fail|conflict|restricted|reject|invalid/.test(raw)) return "error";
  if (/partial|pending|warning|limited|skipped/.test(raw)) return "warning";
  if (/closed|disabled|paused/.test(raw)) return "muted";
  return "pending";
}

function setFutureRadarActionStatus(message, tone = "pending") {
  if (!elements.futureRadarActionStatus) return;
  elements.futureRadarActionStatus.textContent = message;
  elements.futureRadarActionStatus.className = `radar-action-status ${tone}`;
}

function futureRadarRunButton(scanType) {
  return scanType === "deep" ? elements.futureRadarDeepRun : elements.futureRadarRun;
}

function futureRadarRunLabel(scanType) {
  return scanType === "deep" ? elements.futureRadarDeepRunLabel : elements.futureRadarRunLabel;
}

function futureRadarDelayRemaining(scanType) {
  return Math.max(0, Math.ceil(((state.futureRadar.runDelayUntil[scanType] || 0) - Date.now()) / 1000));
}

function syncFutureRadarActiveRuns(dashboard = state.futureRadar.dashboard || {}) {
  state.futureRadar.activeRunTypes = new Set(futureRadarActiveRunTypes(dashboard));
  return state.futureRadar.activeRunTypes;
}

function renderFutureRadarRunAvailability(dashboard = state.futureRadar.dashboard || {}) {
  const activeRunTypes = syncFutureRadarActiveRuns(dashboard);
  FUTURE_RADAR_SCAN_TYPES.forEach((scanType) => {
    const button = futureRadarRunButton(scanType);
    const label = futureRadarRunLabel(scanType);
    if (!button || !label) return;
    const running = state.futureRadar.runStarting[scanType] || activeRunTypes.has(scanType);
    const remaining = futureRadarDelayRemaining(scanType);
    button.disabled = running || remaining > 0;
    button.toggleAttribute("aria-busy", running);
    label.textContent = running
      ? "扫描中..."
      : remaining > 0
        ? "扫描完成"
        : scanType === "deep" ? "深度扫描" : "立即扫描";
    if (remaining > 0) {
      button.title = `本次扫描已完成；${remaining} 秒后可再次扫描，避免误触。`;
    } else {
      button.title = scanType === "deep"
        ? "运行智能发现，寻找新的招聘项目与官方入口。"
        : "优先核对已知官网、ATS、API 与招聘页面，不主动运行 AI 发现。";
    }
  });
}

function startFutureRadarRunDelay(scanType, seconds, message, tone = "healthy") {
  window.clearInterval(state.futureRadar.runDelayTimer[scanType]);
  const duration = Math.max(0, Math.ceil(Number(seconds) || 0));
  state.futureRadar.runDelayUntil[scanType] = Date.now() + duration * 1000;
  const tick = () => {
    const remaining = futureRadarDelayRemaining(scanType);
    if (remaining <= 0) {
      window.clearInterval(state.futureRadar.runDelayTimer[scanType]);
      state.futureRadar.runDelayTimer[scanType] = null;
      state.futureRadar.runDelayUntil[scanType] = 0;
      renderFutureRadarRunAvailability();
      setFutureRadarActionStatus(`${scanType === "deep" ? "Deep Scan" : "Quick Scan"} 已可再次启动。`, "healthy");
      return;
    }
    renderFutureRadarRunAvailability();
    setFutureRadarActionStatus(`${message} · 前端防误触 ${remaining} 秒后解除。`, tone);
  };
  tick();
  state.futureRadar.runDelayTimer[scanType] = window.setInterval(tick, 1000);
}

function futureRadarRunTone(run = {}) {
  const status = String(run.status || "success").toLowerCase();
  if (status === "failed" || status === "error") return "error";
  if (status === "partial_success" || status === "partial" || Number(run.sources_skipped || 0) > 0) return "warning";
  return "healthy";
}

function markFutureRadarRunActive(scanType) {
  const dashboard = state.futureRadar.dashboard || {};
  const activeRunTypes = new Set(futureRadarActiveRunTypes(dashboard));
  activeRunTypes.add(scanType);
  renderFutureRadarDashboard({
    ...dashboard,
    active_run_types: [...activeRunTypes],
    run_in_progress: true,
  });
}

function stopFutureRadarRunStatusPolling(scanType) {
  window.clearTimeout(state.futureRadar.runStatusPollTimer[scanType]);
  state.futureRadar.runStatusPollTimer[scanType] = null;
}

function canPollFutureRadar() {
  return Boolean(state.token && elements.recruitmentDialog?.open && !document.hidden);
}

function readFutureRadarDashboard() {
  return radarPollingGate.dashboard(() => api("/future-radar/dashboard"), state.token);
}

function resumeFutureRadarRunStatusPolling() {
  if (!canPollFutureRadar() || radarPollingGate.suspended()) return;
  FUTURE_RADAR_SCAN_TYPES.forEach((scanType) => {
    if (state.futureRadar.runStatusTracking?.[scanType] || state.futureRadar.activeRunTypes.has(scanType)) {
      state.futureRadar.runStatusTracking ||= { quick: false, deep: false };
      state.futureRadar.runStatusTracking[scanType] = true;
      scheduleFutureRadarRunStatusPoll(scanType);
    }
  });
}

function scheduleFutureRadarRunStatusPoll(scanType) {
  stopFutureRadarRunStatusPolling(scanType);
  if (!canPollFutureRadar() || radarPollingGate.suspended()) return;
  state.futureRadar.runStatusPollTimer[scanType] = window.setTimeout(() => {
    state.futureRadar.runStatusPollTimer[scanType] = null;
    pollFutureRadarRunUntilTerminal(scanType);
  }, radarPollingGate.delay(FUTURE_RADAR_RUN_STATUS_POLL_MS));
}

function latestFutureRadarRunForType(scanType) {
  const matchesType = (run = {}) => {
    const runType = String(run.scan_type || run.run_type || run.trigger_type || "")
      .toLowerCase()
      .replace(/^manual_/, "");
    return runType === scanType;
  };
  return state.futureRadar.runs.find(matchesType)
    || (matchesType(state.futureRadar.dashboard?.last_scan) ? state.futureRadar.dashboard.last_scan : null);
}

async function pollFutureRadarRunUntilTerminal(scanType) {
  if (state.futureRadar.runStatusPollPending[scanType] || !canPollFutureRadar() || radarPollingGate.suspended()) return;
  const sessionToken = state.token;
  state.futureRadar.runStatusPollPending[scanType] = true;
  const scanLabel = scanType === "deep" ? "Deep Scan" : "Quick Scan";
  try {
    const dashboard = await readFutureRadarDashboard();
    if (sessionToken !== state.token || !canPollFutureRadar()) return;
    renderFutureRadarDashboard(dashboard);
    if (futureRadarActiveRunTypes(dashboard).includes(scanType)) {
      setFutureRadarLoading(true, "");
      setFutureRadarActionStatus(`${scanLabel} 仍在服务端扫描；页面会持续跟踪直到完成…`, "running");
      scheduleFutureRadarRunStatusPoll(scanType);
      return;
    }

    stopFutureRadarRunStatusPolling(scanType);
    state.futureRadar.runStatusTracking[scanType] = false;
    setFutureRadarActionStatus(`${scanLabel} 已结束，正在刷新岗位池与扫描记录…`, "running");
    // Quick and Deep finishing together share one final snapshot, not two bursts.
    if (!state.futureRadar.terminalSnapshotPromise) {
      state.futureRadar.terminalSnapshotPromise = loadFutureRadarSnapshot().finally(() => {
        state.futureRadar.terminalSnapshotPromise = null;
      });
    }
    const snapshotReadable = await state.futureRadar.terminalSnapshotPromise;
    if (sessionToken !== state.token || !canPollFutureRadar()) return;
    const terminalRun = latestFutureRadarRunForType(scanType);
    const resultCopy = !snapshotReadable
      ? `${scanLabel} 已在服务端结束；主机会池读取失败，请点击“刷新机会”重试。`
      : terminalRun
        ? `${scanLabel}：${futureRadarRunSuccessCopy(terminalRun, state.futureRadar.totalJobs)}`
        : `${scanLabel} 已在服务端结束；最新岗位池已重新读取。`;
    const tone = snapshotReadable ? futureRadarRunTone(terminalRun || {}) : "warning";
    showToast(resultCopy, 7000);
    startFutureRadarRunDelay(scanType, FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS, resultCopy, tone);
  } catch (error) {
    if (sessionToken !== state.token || !canPollFutureRadar()) {
      stopFutureRadarRunStatusPolling(scanType);
      return;
    }
    markFutureRadarRunActive(scanType);
    setFutureRadarLoading(true, "");
    setFutureRadarActionStatus(radarPollingGate.suspended()
      ? "服务连续不可用，自动跟踪已暂停；请稍后点击刷新机会重试。扫描仍由服务端运行锁管理。"
      : `${scanLabel} 正在等待服务恢复后再次确认；不会重复启动扫描。`, "warning");
    scheduleFutureRadarRunStatusPoll(scanType);
  } finally {
    state.futureRadar.runStatusPollPending[scanType] = false;
  }
}

function startFutureRadarRunStatusPolling(scanType) {
  // A pre-POST idle response (including an in-flight read) cannot end this run.
  radarPollingGate.invalidateDashboard();
  state.futureRadar.runStatusTracking[scanType] = true;
  markFutureRadarRunActive(scanType);
  stopFutureRadarRunStatusPolling(scanType);
  pollFutureRadarRunUntilTerminal(scanType);
}

function renderFutureRadarDashboard(dashboard = state.futureRadar.dashboard) {
  if (!dashboard || !elements.futureRadarDashboard) return;
  state.futureRadar.dashboard = dashboard;
  const metrics = [
    ["NEW", "近7天新增记录", ["new", "new_jobs", "counts.new", "counts.new_jobs", "metrics.new"]],
    ["UPDATED", "更新岗位", ["updated", "updated_jobs", "counts.updated", "counts.updated_jobs", "metrics.updated"]],
    ["CLOSED", "已关闭", ["closed", "closed_jobs", "counts.closed", "counts.closed_jobs", "metrics.closed"]],
    ["PROGRAMS", "招聘项目", ["programs", "program_count", "counts.programs", "counts.recruitment_programs"]],
    ["CLOSING SOON", "即将截止", ["closing_soon", "closing_soon_jobs", "counts.closing_soon"]],
    ["DISCOVERED", "待核验来源记录", ["pending", "pending_jobs", "counts.pending", "counts.pending_verification"]],
    ["VERIFIED", "官网确认记录", ["verified", "verified_jobs", "counts.verified"]],
  ];
  elements.futureRadarDashboard.replaceChildren();
  metrics.forEach(([code, label, paths]) => {
    const clickable = ["NEW", "DISCOVERED", "VERIFIED"].includes(code);
    const card = makeElement(clickable ? "button" : "article", `radar-metric metric-${code.toLowerCase().replaceAll(" ", "-")}`);
    if (clickable) {
      card.type = "button";
      card.title = code === "NEW"
        ? "指标统计近7天新增来源记录；点击查看当前条件下的 NEW 机会（去重且不含已关闭）"
        : `指标统计${label}；点击查看当前条件下的对应机会（去重且不含已关闭）`;
      card.setAttribute("aria-label", card.title);
      card.addEventListener("click", () => showFutureRadarMetric(code));
    }
    card.append(
      makeElement("small", "", code),
      makeElement("strong", "", radarNumber(dashboard, paths).toLocaleString("zh-CN")),
      makeElement("span", "", label),
    );
    elements.futureRadarDashboard.appendChild(card);
  });

  const lastScan = valueAtPaths(dashboard, ["last_scan_at", "last_radar_scan", "last_scan.started_at", "last_run.started_at", "latest_run.started_at"]);
  const lastSuccess = valueAtPaths(dashboard, ["last_success_at", "last_successful_scan", "last_run.finished_at", "latest_success.finished_at"]);
  const derivedHealthy = state.futureRadar.sources.filter((source) => radarStatusClass(source.status || source.health) === "healthy").length;
  const healthy = radarNumber(dashboard, ["healthy_sources", "sources_healthy", "sources.healthy", "source_health.healthy"], derivedHealthy);
  const total = radarNumber(dashboard, ["total_sources", "sources_total", "sources.total", "source_health.total"], state.futureRadar.sources.length);
  const errors = radarNumber(dashboard, ["error_sources", "sources_with_errors", "sources.errors", "source_health.errors"], Math.max(0, total - healthy));
  elements.futureRadarLastScan.textContent = formatRadarTime(lastScan, "等待首次扫描");
  elements.futureRadarLastSuccess.textContent = formatRadarTime(lastSuccess, "尚无成功记录");
  elements.futureRadarSourceHealth.textContent = `${healthy.toLocaleString("zh-CN")} / ${total.toLocaleString("zh-CN")} 健康`;
  const activeRunTypes = futureRadarActiveRunTypes(dashboard);
  const running = activeRunTypes.length > 0;
  renderFutureRadarRunAvailability(dashboard);
  const liveClass = running ? "running" : errors > 0 ? "warning" : total > 0 ? "healthy" : "pending";
  const runningCopy = activeRunTypes.includes("quick") && activeRunTypes.includes("deep")
    ? "Quick / Deep 扫描中"
    : activeRunTypes.includes("deep")
      ? "Deep Scan 扫描中"
      : activeRunTypes.includes("quick")
        ? "Quick Scan 扫描中"
        : activeRunTypes.includes("scheduled")
          ? "自动雷达扫描中"
          : "雷达正在扫描";
  elements.futureRadarLiveState.className = `radar-live-state ${liveClass}`;
  elements.futureRadarLiveState.replaceChildren(
    makeElement("i"),
    document.createTextNode(running ? runningCopy : errors > 0 ? `${errors} 个信源异常` : total > 0 ? "情报链路在线" : "等待雷达状态"),
  );
}

function showFutureRadarMetric(code) {
  if (!["NEW", "DISCOVERED", "VERIFIED"].includes(code)) return;
  const updates = {
    status: DEFAULT_FUTURE_RADAR_STATUS,
    event_type: code === "NEW" ? "NEW" : "",
    verification_status: code === "DISCOVERED" ? "pending" : code === "VERIFIED" ? "verified" : "",
  };
  Object.assign(state.futureRadar.filters, updates);
  if (elements.futureRadarFilterStatus) elements.futureRadarFilterStatus.value = updates.status;
  if (elements.futureRadarFilterEvent) elements.futureRadarFilterEvent.value = updates.event_type;
  if (elements.futureRadarFilterVerification) elements.futureRadarFilterVerification.value = updates.verification_status;
  state.recruitmentTierFilter = "ALL";
  activateFutureRadarTab("jobs");
  return loadFutureRadarJobPage(1, true);
}

function renderFutureRadarOpportunityOverview() {
  renderFutureRadarOpportunityStatus();
  const coverage = elements.futureRadarOpportunityCoverage;
  if (coverage) {
    const copy = futureRadarCoverageCopy(
      state.futureRadar.searchScope, state.futureRadar.searchCoverage, state.futureRadar.searchStatus,
    );
    coverage.classList.toggle("incomplete", copy.incomplete);
    coverage.replaceChildren(makeElement("strong", "", copy.scopeText), makeElement("p", "", copy.resultText));
    const failed = state.futureRadar.searchCoverage?.failed_employers || [];
    if (failed.length) {
      const details = makeElement("details");
      details.append(makeElement("summary", "", "查看未完成的企业"), makeElement("p", "", failed.join(" · ")));
      coverage.appendChild(details);
    }
  }
  const stats = state.futureRadar.opportunityStats || {};
  const counts = stats.verification_status || {};
  const count = (value) => Math.max(0, Number(value) || 0).toLocaleString("zh-CN");
  elements.futureRadarOpportunitySummary?.replaceChildren(
    ...(state.futureRadar.totalCompanies == null ? [] : [makeElement("article", "", `企业分组 ${count(state.futureRadar.totalCompanies)} · 机会 ${count(state.futureRadar.totalJobs)}`)]),
    makeElement("article", "verified", `官网已确认 ${count(counts.verified)}`),
    makeElement("article", "pending", `聊天 / 搜索发现 ${count(counts.pending)}`),
    makeElement("article", "conflicted", `信息有差异 ${count(counts.conflicted)}`),
  );
  if (futureRadarSelectionIsPending()) {
    elements.futureRadarOpportunitySummary?.replaceChildren(
      makeElement("article", "pending", `正在筛选${futureRadarTierLabel()}…`),
    );
  } else if (!state.futureRadar.jobsLoaded) {
    elements.futureRadarOpportunitySummary?.replaceChildren(
      makeElement("article", "pending", state.futureRadar.jobsError ? "主机会池尚未加载，请重试" : "正在读取主机会池…"),
    );
  }
  if (elements.futureRadarOpportunityCount) {
    elements.futureRadarOpportunityCount.textContent = state.futureRadar.jobsLoaded && !futureRadarSelectionIsPending()
      ? count(state.futureRadar.totalJobs) : "—";
  }
  document.querySelectorAll(".recruitment-checks input").forEach((input) => {
    const label = input.closest("label");
    if (!label) return;
    let badge = label.querySelector(".radar-category-count");
    if (!badge) {
      badge = makeElement("small", "radar-category-count");
      label.appendChild(badge);
    }
    const value = stats.category_counts?.[input.value];
    badge.textContent = value == null ? "—" : count(value);
    badge.title = "当前检索条件下的机会数量；不是监控企业数量";
  });
}

function createFutureRadarOpportunityDetail(job) {
  const details = makeElement("details", "job-tier-reason job-opportunity-detail");
  details.dataset.opportunityDetail = String(job.id || "");
  details.appendChild(makeElement("summary", "", "机会详情与来源"));
  const body = makeElement("div", "job-tier-reason-body");
  details.appendChild(body);
  let loaded = false;
  let loading = false;
  const load = async () => {
    if (!details.open || loaded || loading) return;
    loading = true;
    body.replaceChildren(makeElement("small", "", `正在读取完整机会信息… ${futureRadarOpportunityReadHint()}`));
    try {
      const detail = await api(`/future-radar/opportunities/${encodeURIComponent(job.id)}`, {
        timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
      });
      const origin = futureRadarOpportunitySource(detail);
      body.replaceChildren(
        makeElement("strong", "", [detail.company, detail.title].filter(Boolean).join(" · ")),
        makeElement("small", "", origin.description),
      );
      [
        ["岗位介绍", detail.description],
        ["岗位职责", detail.responsibilities],
        ["申请条件", detail.requirements],
      ].forEach(([label, text]) => {
        if (!text) return;
        body.append(makeElement("b", "", label), makeElement("p", "", Array.isArray(text) ? text.join("；") : String(text)));
      });
      const dates = futureRadarOpportunityDateCopy(detail);
      body.append(makeElement("span", "", dates.opening), makeElement("span", "", dates.closing));
      const primaryUrl = futureRadarPublicOpportunityUrl(detail) || futureRadarPublicOpportunityUrl(job);
      if (primaryUrl) {
        const link = makeElement("a", "radar-official-link", recruitmentVerification(detail) === "verified"
          ? "打开招聘公告 / 申请入口 ↗" : "打开原始招聘线索 ↗");
        link.href = primaryUrl;
        link.target = "_blank";
        link.rel = "noreferrer";
        body.appendChild(link);
      }
      const sources = [...(detail.sources || []), ...(detail.discovered_by || []), ...(detail.verified_by || [])];
      const seen = new Set();
      sources.forEach((source) => {
        const name = typeof source === "string" ? source : (source.name || source.source_name || source.source_id || "公开来源");
        const url = futureRadarPublicOpportunityUrl(typeof source === "object" ? { url: source.source_url || source.url } : {});
        const key = `${name}|${url}`;
        if (seen.has(key)) return;
        seen.add(key);
        if (url) {
          const link = makeElement("a", "radar-official-link", `${name} ↗`);
          link.href = url;
          link.target = "_blank";
          link.rel = "noreferrer";
          body.appendChild(link);
        } else body.appendChild(makeElement("span", "", name));
      });
      if (!sources.length) body.appendChild(makeElement("span", "", "来源记录随下一次扫描更新。"));
      loaded = true;
    } catch (error) {
      body.replaceChildren(makeElement("p", "", `详情暂时无法读取：${translateError(error.message)}`));
      const retry = makeElement("button", "job-watch-button", "重试详情");
      retry.type = "button";
      retry.addEventListener("click", load);
      body.appendChild(retry);
    } finally {
      loading = false;
    }
  };
  details.addEventListener("toggle", load);
  return details;
}

function renderFutureRadarPrograms(programs = state.futureRadar.programs) {
  if (!elements.futureRadarPrograms) return;
  elements.futureRadarPrograms.replaceChildren();
  if (!programs.length) {
    elements.futureRadarPrograms.appendChild(makeElement("div", "empty-list", "尚未发现招聘项目。下一轮扫描会继续寻找公开项目级信号。"));
    return;
  }
  programs.forEach((program) => {
    const card = makeElement("article", "radar-entity-card program-card");
    const top = makeElement("div", "radar-entity-top");
    const verification = program.verification_status || program.verification || "pending";
    top.append(
      makeElement("span", "radar-entity-company", program.company || program.company_name || "招聘机构"),
      makeElement("span", `radar-status-badge ${radarStatusClass(verification)}`, radarStatusCopy(verification)),
    );
    const dates = [program.opening_date ? `开放 ${program.opening_date}` : null, program.closing_date ? `截止 ${program.closing_date}` : null].filter(Boolean).join(" · ") || "时间窗待确认";
    const cities = Array.isArray(program.cities) ? program.cities.join(" · ") : (program.region || program.city || "地区待确认");
    const programSources = Array.isArray(program.sources) ? program.sources : [];
    const discoverySources = programSources.filter((source) => source.verification_role === "discovery");
    const verificationSources = programSources.filter((source) => source.verification_role === "verification");
    const sourceRow = makeElement("div", "radar-provenance-row");
    sourceRow.append(
      makeElement("span", "", `发现：${sourceDisplayValue(program.discovered_by || discoverySources || program.source, "公开信号")}`),
      makeElement("span", "", `核验：${sourceDisplayValue(program.verified_by || program.verification_sources || verificationSources, "等待官方来源")}`),
    );
    card.append(
      top,
      makeElement("h4", "", program.program_name || program.name || "招聘项目"),
      makeElement("p", "radar-entity-meta", `${radarStatusCopy(program.status || "unknown")} · ${dates}`),
      makeElement("p", "radar-entity-meta", `${radarNumber(program, ["job_count", "jobs_count", "counts.jobs"], 0)} 个岗位 · ${cities}`),
      sourceRow,
    );
    const linkValue = program.official_url || program.url || program.application_url;
    if (/^https:\/\//.test(linkValue || "")) {
      const link = makeElement("a", "radar-official-link", "打开官方招聘项目 ↗");
      link.href = linkValue;
      link.target = "_blank";
      link.rel = "noreferrer";
      card.appendChild(link);
    }
    elements.futureRadarPrograms.appendChild(card);
  });
}

function eventIdentity(event) {
  return String(event.id ?? event.event_id ?? `${event.event_type || "event"}:${event.detected_at || event.created_at || ""}:${event.external_id || event.entity_id || ""}`);
}

function eventTimestamp(event) {
  return event.detected_at || event.created_at || event.updated_at || event.occurred_at;
}

function renderFutureRadarEvents(events = state.futureRadar.events) {
  if (!elements.futureRadarEvents) return;
  elements.futureRadarEvents.replaceChildren();
  if (!events.length) {
    elements.futureRadarEvents.appendChild(makeElement("div", "empty-list", "情报流正在监听新项目、岗位变化与核验结果。"));
    return;
  }
  events.forEach((event) => {
    const type = String(event.event_type || event.type || "SIGNAL").toUpperCase();
    const card = makeElement("article", `radar-event event-${type.toLowerCase().replaceAll("_", "-")}`);
    const rail = makeElement("span", "radar-event-rail");
    const body = makeElement("div", "radar-event-body");
    const heading = makeElement("div", "radar-event-heading");
    heading.append(
      makeElement("span", `radar-event-type ${radarStatusClass(type)}`, type.replaceAll("_", " ")),
      makeElement("time", "", formatRadarTime(eventTimestamp(event), "时间待确认")),
    );
    const before = event.before_data || event.before;
    const after = event.after_data || event.after;
    const entityRecord = after || before || {};
    const entityTitle = [entityRecord.company, entityRecord.title || entityRecord.program_name].filter(Boolean).join(" · ");
    const entity = event.company || event.entity_name || event.title || event.program_name || entityTitle || event.external_id || "招聘情报更新";
    body.append(heading, makeElement("strong", "", entity));
    const summary = event.summary || event.message || event.description;
    if (summary) body.appendChild(makeElement("p", "", summary));
    const sourceUrl = futureRadarPublicOpportunityUrl(entityRecord) || futureRadarPublicOpportunityUrl(event);
    if (sourceUrl) {
      const sourceLink = makeElement("a", "radar-official-link", "打开公开招聘来源 ↗");
      sourceLink.href = sourceUrl;
      sourceLink.target = "_blank";
      sourceLink.rel = "noreferrer";
      body.appendChild(sourceLink);
    }
    const changedFields = Array.isArray(event.changed_fields)
      ? event.changed_fields
      : (before && after ? [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((key) => JSON.stringify(before[key]) !== JSON.stringify(after[key])) : []);
    if (changedFields.length) {
      const changes = makeElement("details", "radar-event-diff");
      changes.appendChild(makeElement("summary", "", `查看 ${changedFields.length} 项变化`));
      const list = makeElement("div", "radar-event-diff-list");
      changedFields.slice(0, 8).forEach((field) => {
        const row = makeElement("p");
        row.append(
          makeElement("b", "", field),
          makeElement("del", "", before?.[field] ?? "—"),
          makeElement("i", "", "→"),
          makeElement("ins", "", after?.[field] ?? "—"),
        );
        list.appendChild(row);
      });
      changes.appendChild(list);
      body.appendChild(changes);
    }
    card.append(rail, body);
    elements.futureRadarEvents.appendChild(card);
  });
}

function renderFutureRadarSources(sources = state.futureRadar.sources) {
  if (!elements.futureRadarSources) return;
  elements.futureRadarSources.replaceChildren();
  if (!sources.length) {
    elements.futureRadarSources.appendChild(makeElement("div", "empty-list", "Source Registry 尚未返回可展示的信源。"));
    return;
  }
  sources.forEach((source) => {
    const status = source.status || source.health || (source.enabled === false ? "disabled" : "pending");
    const card = makeElement("article", "radar-entity-card source-card");
    const top = makeElement("div", "radar-entity-top");
    top.append(
      makeElement("span", "radar-source-type", String(source.source_type || source.platform || "public_source").replaceAll("_", " ")),
      makeElement("span", `radar-status-badge ${radarStatusClass(status)}`, radarStatusCopy(status)),
    );
    card.append(
      top,
      makeElement("h4", "", source.name || source.company || "公开信源"),
      makeElement("p", "radar-entity-meta", `优先级 ${source.priority ?? "—"} · ${source.trust_level || "UNKNOWN"} · 每 ${source.interval_minutes ?? "—"} 分钟`),
      makeElement("p", "radar-entity-meta", `最近检查 ${formatRadarTime(source.last_checked_at, "尚未检查")} · 最近成功 ${formatRadarTime(source.last_success_at, "尚无记录")}`),
    );
    if (source.latest_article_title) {
      card.appendChild(makeElement(
        "p",
        "radar-source-article",
        `最近文章：${source.latest_article_title} · ${formatRadarTime(source.latest_article_at, "时间待确认")}`,
      ));
    } else if (String(source.source_type || "") === "wechat_public") {
      card.appendChild(makeElement("p", "radar-source-article muted", "最近文章：尚无可验证的公开文章信号"));
    }
    if (source.last_error) card.appendChild(makeElement("p", "radar-source-error", futureRadarSourceErrorCopy(source)));
    elements.futureRadarSources.appendChild(card);
  });
}

function renderFutureRadarRuns(runs = state.futureRadar.runs) {
  if (!elements.futureRadarRuns) return;
  elements.futureRadarRuns.replaceChildren();
  if (!runs.length) {
    elements.futureRadarRuns.appendChild(makeElement("div", "empty-list", "尚无 Radar Run 记录。可以点击“立即扫描”启动第一轮。"));
    return;
  }
  runs.forEach((run) => {
    const status = run.status || "unknown";
    const card = makeElement("article", "radar-entity-card run-card");
    const top = makeElement("div", "radar-entity-top");
    top.append(
      makeElement("span", "radar-run-id", run.id ? `RUN ${String(run.id).slice(0, 12)}` : "RADAR RUN"),
      makeElement("span", `radar-status-badge ${radarStatusClass(status)}`, radarStatusCopy(status)),
    );
    const checked = radarNumber(run, ["sources_checked", "counts.sources_checked"], 0);
    const succeeded = radarNumber(run, ["sources_succeeded", "counts.sources_succeeded"], 0);
    card.append(
      top,
      makeElement("h4", "", formatRadarTime(run.started_at || run.created_at, "扫描时间待确认")),
      makeElement("p", "radar-entity-meta", `${succeeded} / ${checked} 个信源成功 · ${radarNumber(run, ["sources_failed", "counts.sources_failed"], 0)} 个失败`),
      makeElement("p", "radar-run-delta", `＋${radarNumber(run, ["new_jobs", "counts.new_jobs"], 0)} NEW · ↻${radarNumber(run, ["updated_jobs", "counts.updated_jobs"], 0)} UPDATED · ×${radarNumber(run, ["closed_jobs", "counts.closed_jobs"], 0)} CLOSED`),
      makeElement("p", "radar-entity-meta", `${radarNumber(run, ["articles_discovered"], 0)} 篇新文章 · ${radarNumber(run, ["ai_calls"], 0)} 次 AI · ${radarNumber(run, ["model_tokens_used"], 0).toLocaleString("zh-CN")} Tokens`),
    );
    elements.futureRadarRuns.appendChild(card);
  });
}

function activateFutureRadarTab(tab) {
  const next = ["jobs", "programs", "events", "sources", "runs"].includes(tab) ? tab : "jobs";
  state.futureRadar.activeTab = next;
  document.querySelectorAll("[data-radar-tab]").forEach((button) => {
    const active = button.dataset.radarTab === next;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll("[data-radar-panel]").forEach((panel) => {
    panel.classList.toggle("hidden", panel.dataset.radarPanel !== next);
  });
}

function setFutureRadarLoading(loading, errorMessage = "") {
  state.futureRadar.loading = loading;
  elements.futureRadarLoading?.classList.toggle("hidden", !loading);
  if (elements.futureRadarError) {
    elements.futureRadarError.textContent = errorMessage;
    elements.futureRadarError.classList.toggle("hidden", !errorMessage);
  }
  renderFutureRadarOpportunityStatus();
  renderFutureRadarPagination();
}

function mergeFutureRadarEvents(incoming, payload = null) {
  const combined = new Map(state.futureRadar.events.map((event) => [eventIdentity(event), event]));
  incoming.forEach((event) => combined.set(eventIdentity(event), event));
  state.futureRadar.events = [...combined.values()]
    .sort((a, b) => Date.parse(eventTimestamp(b) || 0) - Date.parse(eventTimestamp(a) || 0))
    .slice(0, 100);
  const explicitCursor = valueAtPaths(payload, ["last_event_id", "next_after_event_id", "cursor"]);
  if (explicitCursor !== null) state.futureRadar.lastEventId = explicitCursor;
  else if (incoming.length) {
    const numericIds = incoming.map((event) => Number(event.id ?? event.event_id)).filter(Number.isFinite);
    if (numericIds.length) state.futureRadar.lastEventId = Math.max(...numericIds);
    else {
      const newest = [...incoming].sort((a, b) => Date.parse(eventTimestamp(b) || 0) - Date.parse(eventTimestamp(a) || 0))[0];
      state.futureRadar.lastEventId = newest?.id ?? newest?.event_id ?? state.futureRadar.lastEventId;
    }
  }
}

function renderFutureRadarPagination() {
  if (!elements.futureRadarPagination) return;
  const companyView = (state.futureRadar.jobsError ? state.futureRadar.jobsAppliedView : state.futureRadar.view) === "companies";
  const total = companyView ? state.futureRadar.totalCompanies : state.futureRadar.totalJobs;
  const pageSize = state.futureRadar.jobsError ? state.futureRadar.jobsAppliedPageSize || state.futureRadar.pageSize : state.futureRadar.pageSize;
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  elements.futureRadarPageStatus.textContent = futureRadarSelectionIsPending()
    ? `正在读取${futureRadarTierLabel()}的全池结果…`
    : `第 ${state.futureRadar.jobsError ? state.futureRadar.jobsAppliedPage || 1 : state.futureRadar.page} / ${totalPages} 页 · ${state.futureRadar.jobsError ? "上次成功快照" : "当前筛选"} ${Number(total || 0).toLocaleString("zh-CN")} ${companyView ? "个企业分组" : "个机会"}${companyView ? ` · ${state.futureRadar.totalJobs.toLocaleString("zh-CN")} 个机会` : ""}`;
  elements.futureRadarPagePrev.disabled = state.futureRadar.jobsLoading || Boolean(state.futureRadar.jobsError) || state.futureRadar.page <= 1;
  elements.futureRadarPageNext.disabled = state.futureRadar.jobsLoading || Boolean(state.futureRadar.jobsError) || state.futureRadar.page >= totalPages;
  elements.futureRadarPagination.classList.toggle("hidden", total <= pageSize);
  if (elements.futureRadarOpportunityRefresh) {
    elements.futureRadarOpportunityRefresh.disabled = state.futureRadar.jobsLoading;
    elements.futureRadarOpportunityRefresh.textContent = state.futureRadar.jobsLoading ? "读取机会中…" : "刷新机会 ↻";
  }
}

function recordFutureRadarOpportunityFailure(error) {
  state.futureRadar.jobsError = futureRadarOpportunityErrorCopy(error, state.futureRadar.jobsLoaded);
  return state.futureRadar.jobsError;
}

function applyFutureRadarJobsPayload(payload, query = futureRadarJobsQuery()) {
  const previousError = state.futureRadar.jobsError;
  const params = new URLSearchParams(query);
  const view = (payload.view || params.get("view")) === "companies" ? "companies" : "jobs";
  state.futureRadar.companies = view === "companies" ? radarCollection(payload, ["companies"]) : [];
  state.futureRadar.jobs = view === "jobs" ? radarCollection(payload, ["opportunities", "jobs"]) : [];
  state.futureRadar.deadlineJobs = view === "companies" ? payload.deadline_opportunities || [] : [];
  state.futureRadar.jobsLoaded = true;
  state.futureRadar.jobsError = "";
  state.futureRadar.jobsAppliedQuery = query;
  state.futureRadar.jobsAppliedTier = params.get("tier_code") || "ALL";
  state.futureRadar.jobsAppliedView = view;
  if (previousError && elements.futureRadarError?.textContent === previousError) {
    elements.futureRadarError.textContent = "";
    elements.futureRadarError.classList.add("hidden");
  }
  if (previousError && elements.futureRadarLiveState) {
    elements.futureRadarLiveState.className = "radar-live-state healthy";
    elements.futureRadarLiveState.replaceChildren(makeElement("i"), document.createTextNode("主机会池已恢复"));
  }
  state.futureRadar.totalJobs = radarNumber(payload, ["total_opportunities"], view === "jobs"
    ? radarNumber(payload, ["total"], state.futureRadar.jobs.length)
    : payload.stats?.total_opportunities || 0);
  state.futureRadar.totalCompanies = radarNumber(payload, ["total_companies"], payload.stats?.total_companies ?? null);
  state.futureRadar.page = radarNumber(payload, ["page"], state.futureRadar.page);
  state.futureRadar.jobsAppliedPage = state.futureRadar.page;
  state.futureRadar.pageSize = radarNumber(payload, ["page_size"], state.futureRadar.pageSize);
  state.futureRadar.jobsAppliedPageSize = state.futureRadar.pageSize;
  state.futureRadar.opportunityStats = payload.stats || {};
  state.futureRadar.searchScope = payload.scope || state.futureRadar.searchScope;
  state.futureRadar.searchCoverage = payload.coverage ?? state.futureRadar.searchCoverage;
  state.futureRadar.searchStatus = payload.search_status || state.futureRadar.searchStatus;
  renderFutureRadarOpportunityOverview();
  renderFutureRadarPagination();
}

function futureRadarJobsQuery(page = state.futureRadar.page) {
  return buildFutureRadarJobsQuery({
    page,
    pageSize: state.futureRadar.pageSize,
    filters: { ...state.futureRadar.filters, compact: true, view: state.futureRadar.view || "", tier_code: state.recruitmentTierFilter === "ALL" ? "" : state.recruitmentTierFilter },
    categories: selectedRecruitmentStarfields(),
  });
}

function resetFutureRadarCompanyExpansions() {
  for (const entry of state.futureRadar.companyExpansions?.values() || []) entry.controller?.abort();
  state.futureRadar.companyExpansions = new Map();
}

function futureRadarTierLabel(tier = state.recruitmentTierFilter) {
  return tier === "ALL" ? "全部机会" : tier === "UNRANKED" ? "未评分机会" : tier === "BELOW_PRIORITY" ? "次级机会" : `${tier} 机会`;
}

function futureRadarSelectionIsPending() {
  return state.futureRadar.jobsLoading
    && state.futureRadar.jobsRequestQuery !== state.futureRadar.jobsAppliedQuery;
}

function waitForFutureRadarSelection(signal, delayMs) {
  if (!delayMs || signal.aborted) return Promise.resolve();
  return new Promise((resolve) => {
    let timer;
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    timer = setTimeout(done, delayMs);
    signal.addEventListener("abort", done, { once: true });
  });
}

function syncFutureRadarSourceFilter() {
  if (!elements.futureRadarFilterSource) return;
  const selected = state.futureRadar.filters.source_id;
  elements.futureRadarFilterSource.replaceChildren(new Option("全部信源", ""));
  state.futureRadar.sources.forEach((source) => {
    elements.futureRadarFilterSource.appendChild(new Option(source.name || source.id, source.id));
  });
  elements.futureRadarFilterSource.value = selected;
}

function syncFutureRadarProgramFilter() {
  if (!elements.futureRadarFilterProgram) return;
  const selected = state.futureRadar.filters.program_id;
  elements.futureRadarFilterProgram.replaceChildren(new Option("全部项目", ""));
  state.futureRadar.programs.forEach((program) => {
    const label = [program.company, program.program_name].filter(Boolean).join(" · ") || program.id;
    elements.futureRadarFilterProgram.appendChild(new Option(label, program.id));
  });
  elements.futureRadarFilterProgram.value = selected;
}

function readFutureRadarFilters() {
  state.futureRadar.filters = {
    q: elements.futureRadarFilterQuery.value.trim(),
    company: elements.futureRadarFilterCompany.value.trim(),
    city: elements.futureRadarFilterCity.value.trim(),
    industry: elements.futureRadarFilterIndustry.value.trim(),
    employer_type: elements.futureRadarFilterEmployerType.value.trim(),
    program_id: elements.futureRadarFilterProgram.value,
    status: elements.futureRadarFilterStatus.value,
    verification_status: elements.futureRadarFilterVerification.value,
    source_id: elements.futureRadarFilterSource.value,
    event_type: elements.futureRadarFilterEvent.value,
    sort: elements.futureRadarFilterSort.value,
    opening_after: elements.futureRadarFilterOpeningAfter.value,
    opening_before: elements.futureRadarFilterOpeningBefore.value,
    closing_after: elements.futureRadarFilterClosingAfter.value,
    closing_before: elements.futureRadarFilterClosingBefore.value,
  };
}

function resetFutureRadarFilters() {
  elements.futureRadarFilterForm.reset();
  state.futureRadar.filters = { q: "", company: "", city: "", industry: "", employer_type: "", program_id: "", status: DEFAULT_FUTURE_RADAR_STATUS, verification_status: "", source_id: "", event_type: "", sort: "changed", opening_after: "", opening_before: "", closing_after: "", closing_before: "" };
  state.recruitmentTierFilter = "ALL";
  state.futureRadar.page = 1;
  loadFutureRadarJobPage(1, true);
}

async function loadFutureRadarJobPage(page, force = false, { scroll = true, delayMs = 0 } = {}) {
  if (!state.token) return false;
  const total = state.futureRadar.view === "companies" ? state.futureRadar.totalCompanies : state.futureRadar.totalJobs;
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, state.futureRadar.pageSize)));
  const requested = Math.max(1, Number(page) || 1);
  const nextPage = force ? requested : Math.min(totalPages, requested);
  const query = futureRadarJobsQuery(nextPage);
  if (state.futureRadar.jobsLoading && state.futureRadar.jobsRequestQuery === query) {
    return state.futureRadar.jobsRequestPromise || false;
  }
  if (!force && (state.futureRadar.jobsLoading || nextPage === state.futureRadar.page)) return false;
  const requestId = ++state.futureRadar.jobsRequestId;
  const superseded = Object.assign(new Error("筛选已切换。"), { code: "REQUEST_SUPERSEDED" });
  state.futureRadar.jobsRequestController?.abort(superseded);
  state.futureRadar.pollOpportunityController?.abort(superseded);
  resetFutureRadarCompanyExpansions();
  const controller = new AbortController();
  state.futureRadar.jobsRequestController = controller;
  state.futureRadar.jobsRequestQuery = query;
  state.futureRadar.page = nextPage;
  state.futureRadar.jobsError = "";
  state.futureRadar.jobsLoading = true;
  setFutureRadarLoading(true);
  renderFutureRadarOpportunityOverview();
  if (!state.futureRadar.jobsLoaded || futureRadarSelectionIsPending()) {
    renderRecruitmentJobs(state.futureRadar.jobs);
  }
  const current = () => requestId === state.futureRadar.jobsRequestId && !controller.signal.aborted;
  // Assign the promise before starting the read, so even same-turn callers can
  // share this request. No cached page is treated as the complete ranked pool.
  const promise = Promise.resolve().then(async () => {
    try {
      await waitForFutureRadarSelection(controller.signal, delayMs);
      if (!current()) return false;
      const payload = await api(`/future-radar/opportunities?${query}`, {
        timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
        signal: controller.signal,
      });
      if (!current()) return false;
      state.futureRadar.jobsLoading = false;
      applyFutureRadarJobsPayload(payload, query);
      const displayJobs = futureRadarDisplayJobs(state.recruitmentJobs);
      renderRecruitmentJobs(displayJobs);
      renderRecruitmentDeadlineAlerts(filterRecruitmentByStarfield(displayJobs));
      if (scroll) elements.recruitmentJobs.scrollIntoView({ behavior: "smooth", block: "start" });
      return true;
    } catch (error) {
      if (current()) {
        state.futureRadar.jobsLoading = false;
        setFutureRadarLoading(false, recordFutureRadarOpportunityFailure(error));
        renderFutureRadarOpportunityOverview();
        renderRecruitmentJobs(state.futureRadar.jobs);
      }
      return false;
    } finally {
      if (requestId === state.futureRadar.jobsRequestId) {
        state.futureRadar.jobsLoading = false;
        state.futureRadar.jobsRequestController = null;
        state.futureRadar.jobsRequestPromise = null;
        setFutureRadarLoading(false, elements.futureRadarError?.textContent || "");
      }
    }
  });
  state.futureRadar.jobsRequestPromise = promise;
  return promise;
}

function applyIncrementalRadarMetrics(events) {
  if (!events.length) return;
  const dashboard = { ...(state.futureRadar.dashboard || {}) };
  const countKeys = {
    new_jobs: ["new", ["NEW", "JOB_DISCOVERED"]],
    updated_jobs: ["updated", ["UPDATED", "JOB_UPDATED", "REOPENED"]],
    closed_jobs: ["closed", ["CLOSED", "JOB_CLOSED"]],
    programs: ["programs", ["PROGRAM_DISCOVERED"]],
    verified_jobs: ["verified", ["VERIFIED", "OFFICIAL_SOURCE_FOUND"]],
  };
  Object.entries(countKeys).forEach(([key, [countKey, types]]) => {
    const increase = events.filter((event) => types.includes(String(event.event_type || event.type || "").toUpperCase())).length;
    if (increase) dashboard[key] = radarNumber(dashboard, [key, `counts.${countKey}`], 0) + increase;
  });
  dashboard.last_scan_at = events.map(eventTimestamp).filter(Boolean).sort().at(-1) || dashboard.last_scan_at;
  renderFutureRadarDashboard(dashboard);
}

async function loadFutureRadarSnapshot() {
  if (!state.token) return false;
  const sessionToken = state.token;
  const snapshotRequestId = (state.futureRadar.snapshotRequestId || 0) + 1;
  state.futureRadar.snapshotRequestId = snapshotRequestId;
  // Opportunity results render as soon as that read completes, independently
  // of slower source/program/run metadata. It shares the filter request owner.
  const jobs = loadFutureRadarJobPage(state.futureRadar.page, true, { scroll: false });
  const jobsRequestId = state.futureRadar.jobsRequestId;
  const requests = [
    ["dashboard", readFutureRadarDashboard()],
    ["jobs", jobs],
    ["programs", api("/future-radar/programs")],
    ["events", api("/future-radar/events?limit=50")],
    ["sources", api("/future-radar/sources")],
    ["runs", api("/future-radar/runs")],
  ];
  const results = await Promise.allSettled(requests.map(([, request]) => request));
  if (sessionToken !== state.token || snapshotRequestId !== state.futureRadar.snapshotRequestId) return false;
  const failures = [];
  results.forEach((result, index) => {
    const key = requests[index][0];
    if (result.status === "rejected") {
      failures.push(key);
      return;
    }
    const payload = result.value;
    if (key === "dashboard") {
      state.futureRadar.dashboard = payload;
      renderFutureRadarDashboard(payload);
    } else if (key === "jobs") {
      if (!payload) failures.push("jobs");
    } else if (key === "programs") {
      state.futureRadar.programs = radarCollection(payload, ["programs"]);
      syncFutureRadarProgramFilter();
      renderFutureRadarPrograms();
    } else if (key === "events") {
      mergeFutureRadarEvents(radarCollection(payload, ["events", "changes"]), payload);
      renderFutureRadarEvents();
    } else if (key === "sources") {
      state.futureRadar.sources = radarCollection(payload, ["sources"]);
      syncFutureRadarSourceFilter();
      renderFutureRadarSources();
    } else if (key === "runs") {
      state.futureRadar.runs = radarCollection(payload, ["runs"]);
      renderFutureRadarRuns();
    }
  });
  if (failures.includes("dashboard") && state.futureRadar.sources.length) {
    const healthy = state.futureRadar.sources.filter((source) => radarStatusClass(source.status || source.health) === "healthy").length;
    renderFutureRadarDashboard({ healthy_sources: healthy, total_sources: state.futureRadar.sources.length });
  } else if (state.futureRadar.dashboard) {
    renderFutureRadarDashboard(state.futureRadar.dashboard);
  }
  // Never repaint a saved page under a newer in-flight T/category selection.
  // loadFutureRadarJobPage alone owns opportunity success/failure rendering.
  if (jobsRequestId === state.futureRadar.jobsRequestId) {
    renderFutureRadarOpportunityOverview();
    setFutureRadarLoading(false, state.futureRadar.jobsError || (failures.length ? `部分情报暂时不可用：${failures.join("、")}。现有数据已保留。` : ""));
  }
  return !failures.includes("jobs");
}

async function pollFutureRadarEvents() {
  if (!state.token || state.futureRadar.polling || !elements.recruitmentDialog?.open || document.hidden) return;
  if (radarPollingGate.suspended() || radarPollingGate.delay() > 0) return;
  const sessionToken = state.token;
  const controller = state.futureRadar.jobsLoading ? null : new AbortController();
  state.futureRadar.pollOpportunityController = controller;
  state.futureRadar.polling = true;
  try {
    const cursor = state.futureRadar.lastEventId;
    const query = cursor == null ? "?limit=50" : `?limit=50&after_event_id=${encodeURIComponent(cursor)}`;
    const opportunityQuery = futureRadarJobsQuery();
    const jobsRequestId = state.futureRadar.jobsRequestId;
    let opportunityError = null;
    const [payload, dashboard, opportunityPayload] = await Promise.all([
      api(`/future-radar/events${query}`).catch(() => null),
      state.futureRadar.activeRunTypes.size
        ? readFutureRadarDashboard().catch(() => null)
        : Promise.resolve(null),
      // Chat and search leads must refresh even when there is no verified-only
      // public change event. The unified API owns filtering, ranking and totals.
      state.futureRadar.jobsLoading
        ? Promise.resolve(null)
        : api(`/future-radar/opportunities?${opportunityQuery}`, {
          timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
          signal: controller.signal,
        }).catch((error) => { opportunityError = error; return null; }),
    ]);
    if (sessionToken !== state.token || controller?.signal.aborted
      || state.futureRadar.jobsRequestId !== jobsRequestId
      || futureRadarJobsQuery() !== opportunityQuery) return;
    if (dashboard) renderFutureRadarDashboard(dashboard);
    // Navigation or filter changes made during polling always win over it.
    if (opportunityPayload && !state.futureRadar.jobsLoading
      && state.futureRadar.jobsRequestId === jobsRequestId
      && futureRadarJobsQuery() === opportunityQuery) {
      const companiesView = state.futureRadar.view === "companies";
      const incomingJobs = radarCollection(opportunityPayload, companiesView ? ["companies"] : ["opportunities", "jobs"]);
      const listChanged = !state.futureRadar.jobsLoaded || Boolean(state.futureRadar.jobsError)
        || JSON.stringify(incomingJobs) !== JSON.stringify(companiesView ? state.futureRadar.companies : state.futureRadar.jobs)
        || JSON.stringify(opportunityPayload.stats || {}) !== JSON.stringify(state.futureRadar.opportunityStats);
      applyFutureRadarJobsPayload(opportunityPayload, opportunityQuery);
      if (listChanged) {
        renderRecruitmentJobs(state.futureRadar.jobs);
        renderRecruitmentDeadlineAlerts(state.futureRadar.jobs);
      }
    }
    if (opportunityError && !state.futureRadar.jobsLoading
      && state.futureRadar.jobsRequestId === jobsRequestId
      && futureRadarJobsQuery() === opportunityQuery) {
      const previousError = state.futureRadar.jobsError;
      const message = recordFutureRadarOpportunityFailure(opportunityError);
      setFutureRadarLoading(false, message);
      renderFutureRadarOpportunityOverview();
      if (message !== previousError) renderRecruitmentJobs(state.futureRadar.jobs);
    }
    if (!payload && !opportunityPayload && !state.futureRadar.jobsLoading) throw new Error("Radar poll unavailable");
    const incoming = radarCollection(payload, ["events", "changes"]);
    const knownIds = new Set(state.futureRadar.events.map(eventIdentity));
    const novel = incoming.filter((event) => !knownIds.has(eventIdentity(event)));
    if (incoming.length) mergeFutureRadarEvents(incoming, payload);
    if (novel.length) {
      renderFutureRadarEvents();
      if (payload?.dashboard) renderFutureRadarDashboard(payload.dashboard);
      else applyIncrementalRadarMetrics(novel);
      if (!state.futureRadar.jobsError) {
        elements.futureRadarLiveState.className = "radar-live-state healthy";
        elements.futureRadarLiveState.replaceChildren(makeElement("i"), document.createTextNode(`刚接收 ${novel.length} 条新情报`));
      }
    }
    if (state.futureRadar.jobsError) {
      elements.futureRadarLiveState.className = "radar-live-state warning";
      elements.futureRadarLiveState.replaceChildren(makeElement("i"), document.createTextNode("主机会池暂时无法刷新"));
    }
  } catch (error) {
    if (sessionToken !== state.token || controller?.signal.aborted) return;
    elements.futureRadarLiveState.className = "radar-live-state warning";
    elements.futureRadarLiveState.replaceChildren(makeElement("i"), document.createTextNode("增量链路暂时离线"));
  } finally {
    if (state.futureRadar.pollOpportunityController === controller) state.futureRadar.pollOpportunityController = null;
    if (sessionToken === state.token) state.futureRadar.polling = false;
  }
}

function stopFutureRadarPolling() {
  window.clearInterval(state.futureRadar.pollingTimer);
  state.futureRadar.pollingTimer = null;
  FUTURE_RADAR_SCAN_TYPES.forEach(stopFutureRadarRunStatusPolling);
}

function startFutureRadarPolling() {
  stopFutureRadarPolling();
  if (!state.token || !elements.recruitmentDialog?.open || document.hidden) return;
  resumeFutureRadarRunStatusPolling();
  state.futureRadar.pollingTimer = window.setInterval(pollFutureRadarEvents, FUTURE_RADAR_POLL_INTERVAL_MS);
}

async function runFutureRadarNow(scanType = "quick") {
  if (!FUTURE_RADAR_SCAN_TYPES.includes(scanType)) return;
  if (state.futureRadar.runStarting[scanType] || state.futureRadar.activeRunTypes.has(scanType)) return;
  const existingDelay = futureRadarDelayRemaining(scanType);
  if (existingDelay > 0) {
    setFutureRadarActionStatus(`扫描已完成；前端防误触将在 ${existingDelay} 秒后解除。`, "healthy");
    return;
  }
  state.futureRadar.runStarting[scanType] = true;
  renderFutureRadarRunAvailability();
  setFutureRadarLoading(true, "");
  const scanLabel = scanType === "deep" ? "Deep Scan" : "Quick Scan";
  setFutureRadarActionStatus(scanType === "deep"
    ? "Deep Scan 正在寻找新的招聘项目与官方入口；同类型运行不会重复创建…"
    : "Quick Scan 正在核对已知官网、ATS、API 与招聘页面；同类型运行不会重复创建…", "running");
  try {
    stopFutureRadarRunStatusPolling(scanType);
    radarPollingGate.invalidateDashboard();
    const run = await api("/future-radar/run", {
      method: "POST",
      body: JSON.stringify({ scan_type: scanType }),
      timeoutMs: 120_000,
    });
    radarPollingGate.invalidateDashboard();
    setFutureRadarActionStatus(`${scanLabel} 已返回，正在刷新岗位池、信源健康与变化记录…`, "running");
    const snapshotReadable = await loadFutureRadarSnapshot();
    const runResultCopy = snapshotReadable
      ? futureRadarRunSuccessCopy(run, state.futureRadar.totalJobs)
      : "扫描已完成，但最新岗位池暂时无法重新读取；页面保留现有结果。";
    const resultCopy = `${scanLabel}：${runResultCopy}`;
    const tone = snapshotReadable ? futureRadarRunTone(run) : "warning";
    showToast(resultCopy, 7000);
    startFutureRadarRunDelay(scanType, FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS, resultCopy, tone);
  } catch (error) {
    const copy = futureRadarRunErrorCopy(error, scanType);
    const tone = [409, 429].includes(Number(error.status)) ? "warning" : "error";
    const timedOut = /请求超时|timed?\s*out|timeout/i.test(String(error.message || ""));
    if (timedOut) {
      const trackingCopy = `${scanLabel} 的前端请求窗口已结束；正在查询服务端运行锁，扫描不会被重复启动。`;
      setFutureRadarLoading(true, "");
      showToast(trackingCopy, 6500);
      setFutureRadarActionStatus(trackingCopy, "running");
      startFutureRadarRunStatusPolling(scanType);
    } else if (Number(error.status) === 409) {
      setFutureRadarLoading(true, "");
      showToast(copy, 6500);
      setFutureRadarActionStatus(copy, tone);
      startFutureRadarRunStatusPolling(scanType);
    } else {
      setFutureRadarLoading(false, copy);
      showToast(copy, 6500);
      setFutureRadarActionStatus(copy, tone);
    }
  } finally {
    state.futureRadar.runStarting[scanType] = false;
    renderFutureRadarDashboard(state.futureRadar.dashboard || {});
  }
}

function renderRecruitmentProfile(profile) {
  if (!profile) return;
  elements.recruitmentRoles.value = (profile.desired_roles || []).join("，");
  elements.recruitmentIndustries.value = (profile.industries || []).join("，");
  elements.recruitmentLocations.value = (profile.locations || []).join("，");
  const selectedEmployerTypes = new Set(
    (profile.employer_types || []).map(canonicalStarfieldCode).filter(Boolean),
  );
  document.querySelectorAll(".recruitment-checks input").forEach((input) => {
    input.checked = selectedEmployerTypes.has(input.value);
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
  const companyView = state.futureRadar.jobsAppliedView === "companies";
  if (companyView) jobs = state.futureRadar.deadlineJobs || [];
  elements.recruitmentDeadlineAlerts.replaceChildren();
  if (futureRadarSelectionIsPending()) return;
  const reviewJobs = jobs.filter((job) => ["pending", "conflicted", "failed", "unknown"].includes(recruitmentVerification(job)));
  const verifiedJobs = jobs.filter((job) => recruitmentVerification(job) === "verified").map((job) => ({ ...job, days_left: recruitmentDaysLeft(job) }));
  const urgent = verifiedJobs.filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0 && job.days_left <= 7);
  const dated = verifiedJobs.filter((job) => Number.isInteger(job.days_left) && job.days_left >= 0);
  const heading = document.createElement("strong");
  heading.textContent = urgent.length
    ? `${companyView ? "当前筛选近期时间窗（最多 12 条）" : "本页时间窗预警"} · ${urgent.length} 个官网已确认机会将在 7 天内关闭`
    : dated.length
      ? "时间窗预警 · 暂无 7 天内关闭的已核验机会"
      : "时间窗预警 · 暂无原始公告明确标注截止日期，刷新后将自动核验";
  const list = document.createElement("div");
  urgent.forEach((job) => {
    const item = document.createElement("a");
    item.href = recruitmentJobUrl(job) || "#";
    item.target = "_blank";
    item.rel = "noreferrer";
    item.textContent = `${job.company}｜${job.title}｜${job.days_left === 0 ? "今天截止" : `${job.days_left} 天后截止`}`;
    list.appendChild(item);
  });
  elements.recruitmentDeadlineAlerts.append(heading, list);
  if (reviewJobs.length) {
    const note = document.createElement("small");
    note.className = "recruitment-review-note";
    note.textContent = `本页另有 ${reviewJobs.length} 个聊天 / 搜索线索直接显示在下方；来源标注的日期会单独注明，不作为已确认截止预警。`;
    elements.recruitmentDeadlineAlerts.append(note);
  }
}

function selectedRecruitmentStarfields() {
  return [...document.querySelectorAll(".recruitment-checks input:checked")].map((input) => input.value);
}

function filterRecruitmentByStarfield(jobs) {
  return filterJobsByStarfields(jobs, selectedRecruitmentStarfields());
}

function recruitmentJobUrl(job) {
  return futureRadarPublicOpportunityUrl(job);
}

function recruitmentDaysLeft(job) {
  if (Number.isInteger(job.days_left)) return job.days_left;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(job.closing_date || "")) return null;
  const [year, month, day] = job.closing_date.split("-").map(Number);
  const now = new Date();
  return Math.round((Date.UTC(year, month - 1, day) - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86_400_000);
}

function recruitmentVerification(job) {
  const explicit = job.verification_status || job.verification;
  if (explicit) return String(explicit).toLowerCase();
  const tags = new Set(job.tags || []);
  if (tags.has("待官方核验") || tags.has("待打开核对")) return "pending";
  if (tags.has("链接已验证") || tags.has("标题已验证")) return "verified";
  if (job.last_verified_at) return "verified";
  return "unknown";
}

function sourceDisplayValue(value, fallback) {
  if (Array.isArray(value)) {
    return value.map((item) => typeof item === "string" ? item : (item?.name || item?.source_name || item?.title)).filter(Boolean).join(" · ") || fallback;
  }
  if (value && typeof value === "object") return value.name || value.source_name || value.title || fallback;
  return value || fallback;
}

function futureRadarDisplayJobs() {
  if (!state.futureRadar.jobsLoaded) return [];
  // The unified API already deduplicates legacy, ChatGPT and search records.
  // Appending legacy rows here would corrupt pagination and filtered totals.
  return state.futureRadar.jobs;
}

function finiteRadarScore(value) {
  if (value == null || value === "") return null;
  const score = Number(value);
  return Number.isFinite(score) ? score : null;
}

function recruitmentScoringFactors(job) {
  return formatScoringFactors(job.scoring_factors);
}

function selectRecruitmentTier(tier) {
  if (!["ALL", ...TIER_CODES, "UNRANKED", "BELOW_PRIORITY"].includes(tier)) return false;
  if (state.recruitmentTierFilter === tier && !state.futureRadar.jobsError
    && (state.futureRadar.jobsLoading || state.futureRadar.jobsLoaded)) {
    return state.futureRadar.jobsRequestPromise || false;
  }
  state.recruitmentTierFilter = tier;
  state.futureRadar.page = 1;
  return loadFutureRadarJobPage(1, true, { scroll: false, delayMs: 140 });
}

function renderRecruitmentJobs(jobs) {
  elements.recruitmentJobs.replaceChildren();
  elements.recruitmentJobs.setAttribute("aria-busy", String(state.futureRadar.jobsLoading));
  if (!state.futureRadar.jobsLoaded) {
    const notice = makeElement("div", "empty-list");
    notice.append(
      makeElement("strong", "", state.futureRadar.jobsError ? "主机会池加载失败" : "正在读取主机会池…"),
      makeElement("p", "", state.futureRadar.jobsError || `正在读取聊天线索、搜索发现与官网确认的机会。${futureRadarOpportunityReadHint()}`),
    );
    if (state.futureRadar.jobsError) {
      const retry = makeElement("button", "", "重试加载主机会池");
      retry.type = "button";
      retry.addEventListener("click", () => loadFutureRadarJobPage(1, true, { scroll: false }));
      notice.appendChild(retry);
    }
    elements.recruitmentJobs.appendChild(notice);
    return;
  }
  const showingSavedSnapshot = Boolean(state.futureRadar.jobsError);
  const selectedTier = showingSavedSnapshot
    ? state.futureRadar.jobsAppliedTier || "ALL" : state.recruitmentTierFilter;
  const pendingSelection = futureRadarSelectionIsPending();
  if (showingSavedSnapshot) {
    elements.recruitmentJobs.appendChild(makeElement("p", "radar-load-error", state.futureRadar.jobsError));
  }
  elements.recruitmentJobs.appendChild(createFutureRadarViewSelector());
  const sourceJobs = futureRadarDisplayJobs(jobs);
  const starfieldJobs = showingSavedSnapshot || pendingSelection ? sourceJobs : filterRecruitmentByStarfield(sourceJobs);
  const { priorityJobs, belowPriorityJobs, invalidJobs } = partitionJobsByPriority(starfieldJobs);
  const availableJobs = [...priorityJobs, ...invalidJobs.map((job) => ({ ...job, tier_code: null }))];
  const globalTierCounts = state.futureRadar.opportunityStats?.tier_counts;
  const tierCount = (tier, fallback) => globalTierCounts && Object.hasOwn(globalTierCounts, tier)
    ? Math.max(0, Number(globalTierCounts[tier]) || 0) : fallback;
  const allCount = globalTierCounts
    ? Object.values(globalTierCounts).reduce((sum, count) => sum + Math.max(0, Number(count) || 0), 0)
    : availableJobs.length + belowPriorityJobs.length;
  const tierDefinitions = [
    ["T0", "终极目标", "90–100"], ["T0.5", "准终极", "85–89"],
    ["T1", "核心主申", "80–84"], ["T1.5", "高质量重点", "75–79"],
    ["T2", "值得申请", "70–74"], ["T2.5", "稳健补充", "65–69"], ["T3", "低优先级", "60–64"],
  ];
  const tierOrder = [...TIER_CODES];
  const tierSummary = makeElement("div", "job-tier-summary");
  const allButton = makeElement("button", "job-tier-summary-item ALL", "");
  allButton.type = "button";
  allButton.dataset.tier = "ALL";
  allButton.setAttribute("aria-pressed", String(selectedTier === "ALL"));
  allButton.classList.toggle("active", selectedTier === "ALL");
  allButton.append(
    makeElement("b", "", "全部"),
    makeElement("small", "", `${allCount} 个`),
  );
  allButton.addEventListener("click", () => {
    return selectRecruitmentTier("ALL");
  });
  tierSummary.appendChild(allButton);
  tierDefinitions.forEach(([tier, label, range]) => {
    const count = tierCount(tier, availableJobs.filter((job) => jobTierBucket(job) === tier).length);
    const tierClass = tier.replace(".", "-");
    const button = makeElement("button", `job-tier-summary-item ${tierClass}`);
    button.type = "button";
    button.dataset.tier = tier;
    button.title = `${label}：综合分 ${range}`;
    button.setAttribute("aria-pressed", String(selectedTier === tier));
    button.setAttribute("aria-busy", String(state.futureRadar.jobsLoading && selectedTier === tier));
    button.classList.toggle("active", selectedTier === tier);
    button.append(makeElement("b", "", tier), makeElement("small", "", `${label} · ${count}`));
    button.addEventListener("click", () => {
      return selectRecruitmentTier(tier);
    });
    tierSummary.appendChild(button);
  });
  const unrankedCount = tierCount("UNRANKED", availableJobs.filter((job) => jobTierBucket(job) === "UNRANKED").length);
  const unrankedButton = makeElement("button", "job-tier-summary-item UNRANKED");
  unrankedButton.type = "button";
  unrankedButton.dataset.tier = "UNRANKED";
  unrankedButton.title = "机会直接展示，现有信息不足时不强行归为 T3";
  unrankedButton.setAttribute("aria-pressed", String(selectedTier === "UNRANKED"));
  unrankedButton.classList.toggle("active", selectedTier === "UNRANKED");
  unrankedButton.append(makeElement("b", "", "未评分"), makeElement("small", "", `${unrankedCount} 个`));
  unrankedButton.addEventListener("click", () => {
    return selectRecruitmentTier("UNRANKED");
  });
  tierSummary.appendChild(unrankedButton);
  const belowCount = tierCount("BELOW_PRIORITY", belowPriorityJobs.length);
  if (belowCount || selectedTier === "BELOW_PRIORITY") {
    const belowButton = makeElement("button", "job-tier-summary-item UNRANKED");
    belowButton.type = "button";
    belowButton.dataset.tier = "BELOW_PRIORITY";
    belowButton.setAttribute("aria-pressed", String(selectedTier === "BELOW_PRIORITY"));
    belowButton.classList.toggle("active", selectedTier === "BELOW_PRIORITY");
    belowButton.append(makeElement("b", "", "次级机会"), makeElement("small", "", `低于 60 分 · ${belowCount}`));
    belowButton.addEventListener("click", () => selectRecruitmentTier("BELOW_PRIORITY"));
    tierSummary.appendChild(belowButton);
  }
  elements.recruitmentJobs.appendChild(tierSummary);
  if (pendingSelection) {
    const loading = makeElement("div", "empty-list");
    loading.setAttribute("role", "status");
    loading.setAttribute("aria-live", "polite");
    loading.append(
      makeElement("strong", "", `正在筛选${futureRadarTierLabel()}…`),
      makeElement("p", "", `正在读取整个机会池的对应结果，数量与列表将同时更新。${futureRadarOpportunityReadHint()}可以继续切换其他级别，旧筛选结果不会作为新结果显示。`),
    );
    elements.recruitmentJobs.appendChild(loading);
    return;
  }
  elements.recruitmentJobs.appendChild(
    makeElement("p", "job-tier-legend", `沿用岗位评价规则：T0 ≥90 · T0.5 85–89 · T1 80–84 · T1.5 75–79 · T2 70–74 · T2.5 65–69 · T3 60–64；信息不足列为“未评分”。上方数量统计全部符合当前检索条件的机会，选择 T 级会筛选整个池子，不限当前页${belowPriorityJobs.length ? `；本页 ${belowPriorityJobs.length} 个低于 60 分的机会保留在下方次级区` : ""}。`),
  );
  const displayedView = showingSavedSnapshot ? state.futureRadar.jobsAppliedView : state.futureRadar.view;
  if (displayedView === "companies") {
    const companies = state.futureRadar.companies || [];
    elements.recruitmentJobs.append(
      makeElement("p", "job-tier-filter-result", `本页 ${companies.length} 个企业分组 · ${showingSavedSnapshot ? "上次成功快照（原筛选条件）" : "当前筛选"}共 ${state.futureRadar.totalCompanies || 0} 个企业分组 / ${state.futureRadar.totalJobs} 个机会`),
      makeElement("p", "job-tier-legend", "企业按名称稳定分页；展开只显示符合当前 T 级、星域及搜索条件的机会，岗位沿用所选排序。运营商品牌归组仅用于浏览，不改变实际招聘单位或岗位评分；单位未知的线索分别展示。"),
    );
    if (!companies.length) {
      elements.recruitmentJobs.appendChild(makeElement("div", "empty-list", "当前筛选没有匹配企业。可切换 T 级、星域或搜索条件；所有机会仍保留在池中。"));
    } else companies.forEach((company) => elements.recruitmentJobs.appendChild(createFutureRadarCompanyCard(company)));
    return;
  }
  const displayedJobs = showingSavedSnapshot || selectedTier === "ALL"
    ? availableJobs
    : selectedTier === "UNRANKED"
      ? availableJobs.filter((job) => jobTierBucket(job) === "UNRANKED")
      : availableJobs.filter((job) => jobTierBucket(job) === selectedTier);
  const showBelowPriority = (showingSavedSnapshot || ["ALL", "BELOW_PRIORITY"].includes(selectedTier)) && belowPriorityJobs.length > 0;
  const visibleCount = displayedJobs.length + (showBelowPriority ? belowPriorityJobs.length : 0);
  const matchingTotal = state.futureRadar.jobsLoaded ? state.futureRadar.totalJobs : starfieldJobs.length;
  elements.recruitmentJobs.appendChild(
    makeElement(
      "p",
      "job-tier-filter-result",
      `本页显示 ${visibleCount} 条 · ${showingSavedSnapshot ? `上次成功快照（${futureRadarTierLabel(selectedTier)} · 原筛选条件）` : "当前筛选"}共 ${matchingTotal} 个机会${showingSavedSnapshot || selectedTier === "ALL" ? "" : ` · ${selectedTier}`}`,
    ),
  );
  if (!displayedJobs.length && !showBelowPriority) {
    elements.recruitmentJobs.appendChild(
      makeElement("div", "empty-list", state.recruitmentTierFilter === "ALL" ? "当前检索条件下暂无机会。可调整左侧分类或筛选条件；下一轮扫描的新线索会自动出现。" : `当前没有 ${state.recruitmentTierFilter} 机会；可以切换其他层级或选择“全部”。`),
    );
    return;
  }
  if (!displayedJobs.length && showBelowPriority) {
    elements.recruitmentJobs.appendChild(
      makeElement("div", "empty-list", "本页暂无进入重点池的机会；低优先级事实信号仍保留在下方次级区。"),
    );
  }
  [...tierOrder, "UNRANKED"].forEach((tier) => {
    const tierJobs = displayedJobs.filter((job) => jobTierBucket(job) === tier);
    if (!tierJobs.length) return;
    const group = makeElement("section", "recruitment-tier-group");
    const heading = makeElement("div", "recruitment-tier-heading");
    heading.append(
      makeElement("strong", `job-tier ${tier === "UNRANKED" ? "unranked" : tier.replace(".", "-")}`, tier === "UNRANKED" ? "未评分" : tier),
      makeElement("span", "", `本页 ${tierJobs.length} 个机会`),
    );
    group.appendChild(heading);
    tierJobs.forEach((job) => group.appendChild(createRecruitmentJobCard(job)));
    elements.recruitmentJobs.appendChild(group);
  });
  if (showBelowPriority) {
    const secondary = document.createElement("details");
    secondary.className = "job-tier-reason recruitment-tier-group";
    secondary.appendChild(
      makeElement("summary", "", `未进入重点池 · ${belowPriorityJobs.length} 个（低于 60 分 / 不建议投）`),
    );
    const body = makeElement("div", "job-tier-reason-body");
    body.appendChild(
      makeElement("strong", "", "这些岗位仍属于当前页的事实信号，仅降低视觉优先级；展开后可继续核对和访问官方入口。"),
    );
    belowPriorityJobs.forEach((job) => {
      const card = document.createElement("article");
      card.className = "recruitment-job-card";
      const finalScore = finiteRadarScore(job.job_score ?? job.match_score);
      const dates = futureRadarOpportunityDateCopy(job);
      const daysLeft = dates.verified ? recruitmentDaysLeft(job) : null;
      const deadline = `${dates.closing}${daysLeft == null ? "" : ` · ${daysLeft === 0 ? "今天截止" : `${daysLeft} 天后`}`}`;
      const origin = futureRadarOpportunitySource(job);
      const top = makeElement("div", "job-card-top");
      const labels = document.createElement("div");
      labels.append(
        makeElement("span", "job-company", job.company || "机会发布方"),
        makeElement("span", "job-type", job.employer_type || "重点雇主"),
        makeElement("span", `job-verification ${origin.tone}`, origin.label),
      );
      const rank = makeElement("div", "job-rank");
      rank.append(
        makeElement("span", "job-score", finalScore == null ? "低优先级" : `${finalScore} 分`),
        makeElement("span", "job-tier unranked", "未进入重点池"),
      );
      top.append(labels, rank);
      const bottom = makeElement("div", "job-card-bottom");
      const jobUrl = recruitmentJobUrl(job);
      if (/^https:\/\//.test(jobUrl)) {
        const officialLink = makeElement("a", "", dates.verified ? (job.application_url ? "进入官方申请页 ↗" : "打开原始公告 ↗") : "打开招聘线索 ↗");
        officialLink.href = jobUrl;
        officialLink.target = "_blank";
        officialLink.rel = "noreferrer";
        bottom.appendChild(officialLink);
      } else {
        bottom.appendChild(makeElement("span", "job-link-pending", "官方入口待核验"));
      }
      card.append(
        top,
        makeElement("h4", "", job.title || "机会信号"),
        makeElement("p", "job-meta", `${job.city || job.region || "地点待确认"} · ${job.industry || "行业待确认"} · ${deadline}`),
        makeElement("p", "job-requirements", (job.negative_reasons || [])[0] || "该岗位低于当前重点池门槛，可按需要继续核对。"),
        ...(job.id ? [createFutureRadarOpportunityDetail(job)] : []),
        bottom,
      );
      body.appendChild(card);
    });
    secondary.appendChild(body);
    elements.recruitmentJobs.appendChild(secondary);
  }
}

function createFutureRadarViewSelector() {
  const nav = makeElement("nav", "radar-opportunity-view-switch");
  nav.setAttribute("aria-label", "机会展示方式");
  [["companies", "按企业浏览"], ["jobs", "按岗位浏览"]].forEach(([view, label]) => {
    const button = makeElement("button", "", label);
    button.type = "button";
    button.dataset.opportunityView = view;
    const selected = (state.futureRadar.view || "jobs") === view;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
    button.addEventListener("click", () => selectFutureRadarView(view));
    nav.appendChild(button);
  });
  nav.appendChild(makeElement("span", "", "企业分组不合并岗位，也不代表企业统一 T 级"));
  return nav;
}

function selectFutureRadarView(view) {
  if (!["companies", "jobs"].includes(view)) return false;
  if ((state.futureRadar.view || "jobs") === view && !state.futureRadar.jobsError) {
    return state.futureRadar.jobsRequestPromise || false;
  }
  state.futureRadar.view = view;
  state.futureRadar.page = 1;
  state.futureRadar.pageSize = view === "companies" ? 20 : 50;
  return loadFutureRadarJobPage(1, true, { scroll: false });
}

function createFutureRadarCompanyCard(company) {
  const key = String(company.company_key);
  const entries = state.futureRadar.companyExpansions ||= new Map();
  const signature = JSON.stringify(company);
  let entry = entries.get(key);
  if (!entry) {
    entry = { open: false, loaded: false, loading: false, page: 1, pageSize: 50, jobs: [], requestId: 0 };
    entries.set(key, entry);
  } else if (entry.signature !== signature) {
    entry.controller?.abort();
    entry.loaded = false;
    entry.loading = false;
    entry.page = 1;
    entry.jobs = [];
  }
  entry.signature = signature;
  entry.company = company;
  const details = makeElement("details", "radar-company-card");
  details.dataset.companyKey = key;
  const summary = makeElement("summary", "radar-company-heading");
  const heading = makeElement("div", "radar-company-title");
  heading.append(
    makeElement("h4", "", company.company_name || "招聘单位待确认"),
    makeElement("small", "", company.grouping === "telecom_group" ? "集团 / 品牌展示分组 · 招聘单位逐岗保留"
      : company.grouping === "unknown" ? "单位未知 · 此条线索单独展示" : "同一企业的匹配机会"),
  );
  const count = makeElement("div", "radar-company-count");
  count.append(makeElement("strong", "", `${Number(company.opportunity_count || 0).toLocaleString("zh-CN")} 个机会`), makeElement("small", "", "展开匹配岗位 ▾"));
  summary.append(heading, count);
  const meta = makeElement("div", "radar-company-meta");
  const categoryNames = Object.keys(company.category_counts || {}).map(starfieldLabel);
  if (categoryNames.length) meta.appendChild(makeElement("span", "", categoryNames.join(" · ")));
  const cities = company.cities || [];
  meta.appendChild(makeElement("span", "", cities.length
    ? `${cities.join(" / ")}${company.city_count > cities.length ? ` 等 ${company.city_count} 个地点` : ""}` : "工作地点待确认"));
  meta.appendChild(makeElement("span", "", `官网确认 ${company.verified_count || 0} · 待核验 ${company.discovered_count || 0}${company.program_count ? ` · 含 ${company.program_count} 个招聘项目入口` : ""}`));
  const tiers = makeElement("div", "radar-company-tiers");
  tiers.setAttribute("aria-label", "匹配岗位的 T 级分布，不是企业评级");
  [...TIER_CODES, "UNRANKED", "BELOW_PRIORITY"].forEach((tier) => {
    const total = Number(company.tier_counts?.[tier] || 0);
    if (!total) return;
    tiers.appendChild(makeElement("span", `job-tier ${TIER_CODES.includes(tier) ? tier.replace(".", "-") : "unranked"}`,
      `${tier === "UNRANKED" ? "未评分" : tier === "BELOW_PRIORITY" ? "次级" : tier} · ${total}`));
  });
  heading.append(meta, tiers);
  const body = makeElement("div", "radar-company-jobs");
  entry.details = details;
  entry.body = body;
  details.append(summary, body);
  details.open = entry.open;
  const toggle = () => {
    if (entries.get(key) !== entry || entry.details !== details) return;
    entry.open = details.open;
    if (!entry.open) {
      entry.controller?.abort();
      entry.loading = false;
      return;
    }
    if (entry.loaded) return renderFutureRadarCompanyJobs(entry);
    return loadFutureRadarCompanyJobs(key, entry.page);
  };
  details.addEventListener("toggle", toggle);
  if (entry.open) {
    if (entry.loaded || entry.loading) renderFutureRadarCompanyJobs(entry);
    else loadFutureRadarCompanyJobs(key, entry.page);
  }
  return details;
}

function renderFutureRadarCompanyJobs(entry) {
  const body = entry.body;
  body.replaceChildren();
  body.setAttribute("aria-busy", String(entry.loading));
  if (entry.loading) {
    body.appendChild(makeElement("p", "radar-loading", `正在读取该企业在当前筛选范围内的岗位… ${futureRadarOpportunityReadHint()}`));
    return;
  }
  if (entry.error) {
    body.appendChild(makeElement("p", "radar-load-error", entry.error));
    const retry = makeElement("button", "job-watch-button", "重试读取企业岗位");
    retry.type = "button";
    retry.addEventListener("click", () => loadFutureRadarCompanyJobs(entry.company.company_key, entry.page));
    body.appendChild(retry);
    return;
  }
  const pages = Math.max(1, Math.ceil(entry.total / entry.pageSize));
  body.appendChild(makeElement("p", "job-tier-filter-result", `匹配范围内 ${entry.total} 个机会 · 本页 ${entry.jobs.length} 条 · 第 ${entry.page} / ${pages} 页`));
  body.appendChild(makeElement("p", "job-tier-legend", "以下招聘单位、T 级、日期、来源与详情均按具体岗位保留；分组名称不是用工主体或评级依据。"));
  if (!entry.jobs.length) body.appendChild(makeElement("p", "empty-list", "该企业在当前筛选范围内暂无机会，可刷新企业列表。"));
  entry.jobs.forEach((job) => body.appendChild(createRecruitmentJobCard(job)));
  if (pages > 1) {
    const pager = makeElement("nav", "radar-pagination");
    pager.setAttribute("aria-label", `${entry.company.company_name} 的岗位分页`);
    const previous = makeElement("button", "", "← 上一页岗位");
    const next = makeElement("button", "", "下一页岗位 →");
    previous.type = next.type = "button";
    previous.disabled = entry.page <= 1;
    next.disabled = entry.page >= pages;
    const go = (page) => {
      const promise = loadFutureRadarCompanyJobs(entry.company.company_key, page);
      body.scrollIntoView({ behavior: "smooth", block: "start" });
      return promise;
    };
    previous.addEventListener("click", () => go(entry.page - 1));
    next.addEventListener("click", () => go(entry.page + 1));
    pager.append(previous, makeElement("span", "", `第 ${entry.page} / ${pages} 页 · 每页 ${entry.pageSize} 条`), next);
    body.appendChild(pager);
  }
}

async function loadFutureRadarCompanyJobs(companyKey, page = 1) {
  const radar = state.futureRadar;
  const entry = radar.companyExpansions?.get(companyKey);
  if (!state.token || radar.view !== "companies" || radar.jobsLoading || radar.jobsError || !entry?.open) return false;
  const parentQuery = radar.jobsAppliedQuery;
  const owner = radar.jobsRequestId;
  const query = buildFutureRadarCompanyJobsQuery({ parentQuery, companyKey, page, pageSize: entry.pageSize });
  if (entry.loading && entry.query === query) return entry.promise || false;
  entry.controller?.abort();
  const controller = new AbortController();
  const requestId = ++entry.requestId;
  entry.controller = controller;
  entry.query = query;
  entry.page = Math.max(1, Number(page) || 1);
  entry.loaded = false;
  entry.loading = true;
  entry.error = "";
  renderFutureRadarCompanyJobs(entry);
  const current = () => radar.companyExpansions?.get(companyKey) === entry
    && requestId === entry.requestId && !controller.signal.aborted && entry.open
    && radar.view === "companies" && !radar.jobsLoading
    && radar.jobsRequestId === owner && radar.jobsAppliedQuery === parentQuery;
  const promise = Promise.resolve().then(async () => {
    try {
      const payload = await api(`/future-radar/opportunities?${query}`, {
        timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS, signal: controller.signal,
      });
      if (!current()) return false;
      entry.jobs = radarCollection(payload, ["opportunities", "jobs"]);
      entry.total = radarNumber(payload, ["total_opportunities", "total"], entry.jobs.length);
      entry.page = radarNumber(payload, ["page"], entry.page);
      entry.pageSize = radarNumber(payload, ["page_size"], entry.pageSize);
      entry.loaded = true;
      entry.loading = false;
      renderFutureRadarCompanyJobs(entry);
      return true;
    } catch (error) {
      if (current()) {
        entry.loading = false;
        entry.error = futureRadarOpportunityErrorCopy(error, false);
        renderFutureRadarCompanyJobs(entry);
      }
      return false;
    } finally {
      if (requestId === entry.requestId) {
        entry.loading = false;
        entry.controller = null;
        entry.promise = null;
      }
    }
  });
  entry.promise = promise;
  return promise;
}

function createRecruitmentJobCard(job) {
  const card = document.createElement("article");
  card.className = "recruitment-job-card";
  const dateCopy = futureRadarOpportunityDateCopy(job);
  const daysLeft = dateCopy.verified ? recruitmentDaysLeft(job) : null;
  const deadline = `${dateCopy.closing}${daysLeft == null ? "" : ` · ${daysLeft === 0 ? "今天截止" : `${daysLeft} 天后`}`}`;
  const tierBucket = jobTierBucket(job);
  const tierCode = TIER_CODES.includes(tierBucket) ? tierBucket : null;
  const belowPriority = tierBucket === "BELOW_PRIORITY";
  const isRecruitmentProgram = job.listing_kind === "recruitment_program" || job.scoring_status === "unscored_program_listing";
  const finalScore = tierCode || belowPriority ? finiteRadarScore(job.job_score ?? job.match_score) : null;
  const verification = recruitmentVerification(job);
  const origin = futureRadarOpportunitySource(job);
  const top = makeElement("div", "job-card-top");
  const labels = document.createElement("div");
  labels.append(
    makeElement("span", "job-company", job.company || "机会发布方"),
    makeElement("span", "job-type", job.employer_type || "重点雇主"),
    makeElement("span", `job-verification ${origin.tone}`, origin.label),
  );
  if (isRecruitmentProgram) labels.appendChild(makeElement("span", "job-type", "招聘项目"));
  if (verification === "conflicted") labels.appendChild(makeElement("span", "job-verification warning", "信息有差异"));
  const rank = makeElement("div", "job-rank");
  if (tierCode) {
    rank.append(
      makeElement("span", "job-score", finalScore == null ? "已评分" : `${finalScore} 分`),
      makeElement("span", `job-tier ${tierCode.replace(".", "-")}`, tierCode),
    );
  } else if (belowPriority) {
    rank.append(makeElement("span", "job-score", finalScore == null ? "低优先级" : `${finalScore} 分`), makeElement("span", "job-tier unranked", "未进入重点池"));
  } else {
    rank.appendChild(makeElement("span", "job-tier unranked", "未评分"));
  }
  top.append(labels, rank);
  const bottom = makeElement("div", "job-card-bottom");
  const jobUrl = recruitmentJobUrl(job);
  if (/^https:\/\//.test(jobUrl)) {
    const officialLink = makeElement("a", "", verification === "verified" ? (job.application_url ? "进入官方申请页 ↗" : "打开原始公告 ↗") : "打开招聘线索 ↗");
    officialLink.href = jobUrl;
    officialLink.target = "_blank";
    officialLink.rel = "noreferrer";
    bottom.appendChild(officialLink);
  } else {
    bottom.appendChild(makeElement("span", "job-link-pending", "官方入口待核验"));
  }
  const reason = document.createElement("details");
  reason.className = "job-tier-reason";
  reason.appendChild(makeElement("summary", "", tierCode ? "为什么是这个级别" : "评分状态与适配信息"));
  const reasonBody = makeElement("div", "job-tier-reason-body");
  const scoreGrid = makeElement("div", "job-fact-grid job-score-factor-grid");
  const factorScores = [
    ["FINAL TIER", tierCode || (belowPriority ? "未进入重点池" : "未评分")],
    ["FINAL SCORE", finalScore == null ? "—" : `${finalScore} / 100`],
    ["EMPLOYER SCORE", tierCode ? finiteRadarScore(job.employer_score) : null],
    ["ROLE SCORE", tierCode ? finiteRadarScore(job.role_score) : null],
    ["CAREER VALUE", tierCode ? finiteRadarScore(job.career_value_score) : null],
    ["JOB CONDITIONS", tierCode ? finiteRadarScore(job.job_condition_score) : null],
  ];
  factorScores.forEach(([label, value]) => {
    scoreGrid.appendChild(makeElement("span", "", `${label} · ${value == null ? "—" : value}`));
  });
  const positiveList = makeElement("ul", "positive-reasons");
  (job.positive_reasons || []).slice(0, 3).forEach((item) => positiveList.appendChild(makeElement("li", "", item)));
  const negativeList = makeElement("ul", "negative-reasons");
  (job.negative_reasons || []).slice(0, 2).forEach((item) => negativeList.appendChild(makeElement("li", "", item)));
  const flags = (job.fit_tags || []).length
    ? `适配标签：${job.fit_tags.join(" · ")}`
    : "适配标签：等待更多官方岗位信息";
  const conciseFactors = tierCode ? recruitmentScoringFactors(job) : [];
  const factorList = makeElement("ul", "score-factor-reasons");
  conciseFactors.forEach((item) => factorList.appendChild(makeElement("li", "", item)));
  const organizationFactors = tierCode && job.organization_assessment
    ? formatOrganizationAssessment(job.organization_assessment) : [];
  const organizationList = makeElement("ul", "score-factor-reasons");
  organizationFactors.forEach((item) => organizationList.appendChild(makeElement("li", "", item)));
  reasonBody.append(
    scoreGrid,
    makeElement("strong", "", isRecruitmentProgram ? "这是招聘项目入口，具体岗位尚未拆分；不以公司等级代替岗位 T 级" : tierCode || belowPriority ? "按现有公司与岗位信息应用统一 T 级规则；排序不等于官网确认" : "岗位信息不足，暂未生成 T 级；机会仍然在主池展示"),
    ...(organizationFactors.length ? [organizationList] : []),
    ...(conciseFactors.length ? [makeElement("span", "", "评分依据"), factorList] : []),
    makeElement("span", "", "主要加分"),
    positiveList,
    makeElement("span", "", "主要减分 / 待核对"),
    negativeList,
    makeElement("small", "", flags),
  );
  reason.appendChild(reasonBody);
  const programName = job.program_name || job.recruitment_program?.name || job.program?.name;
  const facts = makeElement("div", "job-fact-grid");
  facts.append(
    makeElement("span", "", `项目：${programName || "尚未归入招聘项目"}`),
    makeElement("span", "", `状态：${radarStatusCopy(job.status || "unknown")}`),
    makeElement("span", "", `首次发现：${formatRadarTime(job.first_seen_at, "待记录")}`),
    makeElement("span", "", `最近变化：${formatRadarTime(job.last_changed_at || job.updated_at || job.last_verified_at, "待记录")}`),
  );
  const provenance = makeElement("div", "job-provenance");
  provenance.append(
    makeElement("span", "", `DISCOVERED BY · ${sourceDisplayValue(job.discovered_by || job.discovery_sources || job.source, "公开信号")}`),
    makeElement("span", "", verification === "verified" ? `VERIFIED BY · ${sourceDisplayValue(job.verified_by || job.verification_sources, "官方来源")}` : origin.description),
  );
  card.append(
    top,
    makeElement("h4", "", job.title || "机会信号"),
    makeElement("p", "job-meta", `${job.city || job.region || "地点待确认"} · ${job.industry || "行业待确认"} · ${deadline}`),
    makeElement("p", "job-requirements", job.requirements || "请打开官方公告核对申请条件。"),
    facts,
    provenance,
    ...(job.id ? [createFutureRadarOpportunityDetail(job)] : []),
    reason,
    bottom,
  );
  const watchButton = makeElement("button", "job-watch-button", "跟踪此公告变化");
  watchButton.type = "button";
  watchButton.disabled = !/^https:\/\//.test(jobUrl);
  if (watchButton.disabled) watchButton.title = "等待官方公开链接核验后可建立监控";
  watchButton.addEventListener("click", () => addRecruitmentWatchFromJob({ ...job, url: jobUrl }, watchButton));
  bottom.appendChild(watchButton);
  return card;
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
  if (!state.token) return null;
  let legacyData = null;
  try {
    const [profile, data, watchData] = await Promise.all([
      api("/recruitment/profile"),
      api("/recruitment/jobs"),
      api("/recruitment/watches").catch(() => ({ watches: [] })),
    ]);
    legacyData = data;
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
    renderFutureRadarOpportunityStatus();
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    renderFutureRadarOpportunityStatus();
    renderRecruitmentSyncStatus(state.recruitmentSyncStatus);
  }
  // Profile/watch compatibility requests must not prevent the main pool from
  // loading, and their success does not mean the main pool loaded correctly.
  const opportunitiesReadable = await loadFutureRadarSnapshot();
  return opportunitiesReadable ? legacyData : null;
}

async function refreshRecruitmentSource() {
  const idleLabel = RECRUITMENT_REFRESH_LABEL;
  elements.recruitmentRefresh.disabled = true;
  elements.recruitmentRefresh.classList.add("is-syncing");
  elements.recruitmentRefresh.setAttribute("aria-busy", "true");
  elements.recruitmentRefresh.textContent = "候选源同步中…";
  elements.recruitmentError.textContent = "";
  setRecruitmentStatus("正在同步已有候选来源与官网哨站；如需发现新入口，可按需启动 Deep Scan…");
  setFutureRadarActionStatus("正在同步已有候选来源和官网哨站；本次不主动运行 Deep Discovery…", "running");
  try {
    const results = await Promise.allSettled([
      api("/recruitment/refresh", { method: "POST", timeoutMs: 90000 }),
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
    const webSearchCopy = "Deep Discovery：本次未请求；可使用 Deep Scan 按需启动";
    const watchResult = watchesOk ? results[1].value : null;
    const checkedWatches = Number(watchResult?.checked ?? watchResult?.count ?? watchResult?.refreshed);
    const watchCopy = watchesOk
      ? (Number.isFinite(checkedWatches) ? `官网哨站：本次核对 ${checkedWatches.toLocaleString("zh-CN")} 个` : "官网哨站：本次已刷新")
      : "官网哨站：本次未完成";
    const bridgeCopy = refreshedData
      ? "已读取受控桥当前状态，未将历史 AI 结果计作本轮"
      : "受控桥状态本次未能重新读取，未将历史 AI 结果计作本轮";
    const refreshCopy = `${sourceCopy}；${watchCopy}；${webSearchCopy}。${bridgeCopy}。`;
    const partial = results.some((result) => result.status === "rejected") || !refreshedData;
    setFutureRadarActionStatus(refreshCopy, partial ? "warning" : "healthy");
    showToast(refreshCopy, 7000);
  } catch (error) {
    const copy = `公开源或官网哨站同步未完成：${translateError(error.message)}`;
    elements.recruitmentError.textContent = copy;
    setRecruitmentStatus("公开来源同步未完成；当前列表保持不变");
    setFutureRadarActionStatus(`${copy} 当前岗位池没有被清空。`, "error");
    showToast("公开源或官网哨站同步未完成；当前岗位列表没有被清空，请稍后重试。", 5500);
  } finally {
    elements.recruitmentRefresh.disabled = false;
    elements.recruitmentRefresh.classList.remove("is-syncing");
    elements.recruitmentRefresh.removeAttribute("aria-busy");
    elements.recruitmentRefresh.textContent = idleLabel;
  }
}

async function openRecruitment() {
  updateProductSwitchers("recruitment");
  elements.recruitmentError.textContent = "";
  if (!elements.recruitmentDialog.open) {
    elements.recruitmentDialog.showModal();
    playSceneEntry(elements.recruitmentDialog);
  }
  await refreshRecruitment();
  startFutureRadarPolling();
}

let recruitmentAutoFilterTimer = null;
const futureRadarProfileReload = createCoalescedRadarReload({
  isBusy: () => state.futureRadar.loading,
  defer: (task) => window.setTimeout(task, 120),
  reload: () => {
    state.futureRadar.page = 1;
    renderFutureRadarPagination();
    return loadFutureRadarJobPage(1, true, { scroll: false });
  },
  onError: (error) => setFutureRadarLoading(false, translateError(error?.message || String(error))),
});

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
    futureRadarProfileReload.request();
    const data = await Promise.race([
      api("/recruitment/jobs"),
      new Promise((_, reject) => setTimeout(() => reject(new Error("岗位匹配请求超时，请稍后重试。")), 12000)),
    ]);
    state.recruitmentJobs = data.jobs || [];
    const displayJobs = futureRadarDisplayJobs(state.recruitmentJobs);
    renderRecruitmentJobs(displayJobs);
    renderRecruitmentDeadlineAlerts(filterRecruitmentByStarfield(displayJobs));
    renderHomeRecruitmentAlerts(state.recruitmentJobs, state.recruitmentWatches);
    renderRecruitmentMonitors(data.monitor_pools || []);
    renderFutureRadarOpportunityStatus();
    if (!silent) showToast("坐标已保存，Radar 正在按新画像重算。", 3500);
  } catch (error) {
    elements.recruitmentError.textContent = translateError(error.message);
    renderFutureRadarOpportunityStatus();
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
$("recruitment-close").addEventListener("click", () => {
  stopFutureRadarPolling();
  elements.recruitmentDialog.close();
});
elements.recruitmentDialog.addEventListener("close", stopFutureRadarPolling);
elements.futureRadarRun.addEventListener("click", () => runFutureRadarNow("quick"));
elements.futureRadarDeepRun.addEventListener("click", () => runFutureRadarNow("deep"));
elements.futureRadarPagePrev.addEventListener("click", () => loadFutureRadarJobPage(state.futureRadar.page - 1));
elements.futureRadarPageNext.addEventListener("click", () => loadFutureRadarJobPage(state.futureRadar.page + 1));
elements.futureRadarOpportunityRefresh.addEventListener("click", () => {
  radarPollingGate.resume();
  resumeFutureRadarRunStatusPolling();
  loadFutureRadarJobPage(state.futureRadar.page, true, { scroll: false });
});
elements.futureRadarFilterForm.addEventListener("submit", (event) => {
  event.preventDefault();
  readFutureRadarFilters();
  state.recruitmentTierFilter = "ALL";
  state.futureRadar.page = 1;
  loadFutureRadarJobPage(1, true);
});
elements.futureRadarFilterReset.addEventListener("click", resetFutureRadarFilters);
document.querySelectorAll("[data-radar-tab]").forEach((button) => {
  button.addEventListener("click", () => activateFutureRadarTab(button.dataset.radarTab));
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopFutureRadarPolling();
    return;
  }
  if (elements.recruitmentDialog.open) {
    pollFutureRadarEvents();
    startFutureRadarPolling();
  }
});
elements.recruitmentRefresh.addEventListener("click", refreshRecruitmentSource);
elements.recruitmentForm.addEventListener("submit", saveRecruitment);
document.querySelectorAll(".recruitment-checks input").forEach((input) => {
  input.addEventListener("change", () => {
    state.recruitmentTierFilter = "ALL";
    renderRecruitmentJobs(state.recruitmentJobs);
    renderRecruitmentDeadlineAlerts(filterRecruitmentByStarfield(state.recruitmentJobs));
    const selected = selectedRecruitmentStarfields();
    setRecruitmentStatus(selected.length ? `已即时筛选：${selected.map(starfieldLabel).join(" · ")}；正在同步保存你的星域坐标…` : "已显示全部信号星域；正在同步保存坐标…");
    scheduleRecruitmentAutoFilter();
  });
});
[elements.recruitmentRoles, elements.recruitmentIndustries, elements.recruitmentLocations].forEach((input) => {
  input.addEventListener("change", scheduleRecruitmentAutoFilter);
});
elements.recruitmentWatchForm.addEventListener("submit", addRecruitmentWatch);
renderFutureRadarDashboard({});
renderFutureRadarOpportunityOverview();
renderFutureRadarPrograms([]);
renderFutureRadarEvents([]);
renderFutureRadarSources([]);
renderFutureRadarRuns([]);
renderFutureRadarPagination();
activateFutureRadarTab(state.futureRadar.activeTab);
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
setupProductSwitchers();
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
  state.pendingLaunch = await storage.get(STORAGE_KEYS.pendingProduct);
  productLaunchReady = true;
  if (Capacitor.isNativePlatform() && !configuredApiBase) {
    elements.authError.textContent = "移动端构建尚未配置正式 HTTPS API 地址。";
  }
  if (!state.token) {
    if (queuedProductLaunch) {
      const queuedProduct = queuedProductLaunch;
      queuedProductLaunch = null;
      await launchProduct(queuedProduct);
      return;
    }
    const publicProduct = state.pendingLaunch || state.activeProduct;
    if (["oblivion", "resonance", "trace"].includes(publicProduct)) {
      window.setTimeout(() => launchProduct(publicProduct), 80);
    } else if (state.pendingLaunch) {
      showPendingProductAuth(state.pendingLaunch);
    }
    return;
  }
  try {
    state.user = await api("/auth/me");
    await enterApp();
  } catch (_) { await logout(false); }
})();
