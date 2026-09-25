const $ = (id) => document.getElementById(id);
const money = (cents, currency = "AUD") => new Intl.NumberFormat("zh-CN", { style: "currency", currency, maximumFractionDigits: 2 }).format((Number(cents) || 0) / 100);
const date = (value) => value ? new Date(value).toLocaleDateString("zh-CN") : "—";
const iso = (value) => value ? new Date(value).toISOString() : new Date().toISOString();
const READER_PAGE_SIZE = 100;
const MAX_TRANSLATION_CACHE = 800;

const SENTENCE_ABBREVIATIONS = new Set([
  "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "fig", "no", "dept", "inc", "ltd", "e.g", "i.e",
]);

function sentencePeriodEnds(text, index) {
  if (text[index] !== ".") return true;
  if (/\d/.test(text[index - 1] || "") && /\d/.test(text[index + 1] || "")) return false;
  if (/[A-Z]/.test(text[index - 1] || "") && /^[A-Z]\./.test(text.slice(index + 1))) return false;
  const prefix = text.slice(0, index + 1);
  const rawToken = prefix.match(/([A-Za-z](?:[A-Za-z.]*)?)\.$/)?.[1] || "";
  const token = rawToken.toLowerCase();
  if (SENTENCE_ABBREVIATIONS.has(token)) return false;
  if (/^(?:[a-z]\.){2,}$/i.test(token + ".")) return false;
  return true;
}

export function sentenceRanges(text) {
  const source = String(text || "");
  const ranges = [];
  let start = 0;
  const push = (end) => {
    let left = start; let right = end;
    while (left < right && /\s/.test(source[left])) left += 1;
    while (right > left && /\s/.test(source[right - 1])) right -= 1;
    if (right > left) ranges.push({ index: ranges.length, start: left, end: right, text: source.slice(left, right) });
    start = end;
  };
  for (let index = 0; index < source.length; index += 1) {
    if (!/[.!?。！？]/.test(source[index])) continue;
    if (source[index] === "." && !sentencePeriodEnds(source, index)) continue;
    let end = index + 1;
    while (end < source.length && /["'”’）)\]}]/.test(source[end])) end += 1;
    push(end);
    index = end - 1;
  }
  if (start < source.length) push(source.length);
  return ranges;
}

export function sentenceAtSelection(text, start, end) {
  const source = String(text || "");
  const safeStart = Math.max(0, Math.min(Number(start) || 0, source.length));
  const safeEnd = Math.max(safeStart, Math.min(Number(end) || safeStart, source.length));
  const ranges = sentenceRanges(source);
  if (!ranges.length) return { index: 0, start: 0, end: source.length, text: source.trim() };
  return ranges.find((item) => safeStart >= item.start && safeStart < item.end)
    || ranges.find((item) => safeEnd > item.start && safeEnd <= item.end)
    || ranges.find((item) => item.start >= safeStart)
    || ranges[ranges.length - 1];
}

export function sentenceAroundSelection(text, start, end) {
  return sentenceAtSelection(text, start, end).text;
}

export function translationTarget(selection, scope) {
  const paragraphText = String(selection?.paragraph_text || "");
  if (scope === "paragraph") return { text: paragraphText.trim(), sentenceIndex: null, paragraphEnd: selection?.paragraph_position };
  if (scope === "sentence") {
    const sentence = sentenceAtSelection(paragraphText, selection?.start_offset, selection?.end_offset);
    return { text: sentence.text, sentenceIndex: sentence.index, start: sentence.start, end: sentence.end, paragraphEnd: selection?.paragraph_position };
  }
  return {
    text: String(selection?.quote || "").trim(), sentenceIndex: null,
    start: Number(selection?.start_offset) || 0, end: Number(selection?.end_offset) || 0, paragraphEnd: Number(selection?.paragraph_end ?? selection?.paragraph_position ?? 0),
  };
}

export function classifySelectionScope(selection) {
  const quote = String(selection?.quote || "").trim();
  if (!quote) return "selection";
  if (Number(selection?.paragraph_position) !== Number(selection?.paragraph_end ?? selection?.paragraph_position)) return "selection";
  const paragraph = String(selection?.paragraph_text || "");
  const start = Number(selection?.start_offset) || 0;
  const end = Number(selection?.end_offset) || 0;
  const paragraphStart = paragraph.search(/\S/u);
  const paragraphEnd = paragraph.search(/\s*$/u);
  if (start === paragraphStart && end === paragraphEnd) return "paragraph";
  const sentence = sentenceAtSelection(paragraph, start, end);
  if (start === sentence.start && end === sentence.end) return "sentence";
  if (isSingleEnglishWord(quote)) return "word";
  return "selection";
}

export function createSelectionSnapshot({ material, rows, startPosition, endPosition, start, end, quote }) {
  const selectedRows = Array.isArray(rows) ? rows : [];
  if (!material || !selectedRows.length) return null;
  const first = selectedRows[0];
  const containingSentence = sentenceAtSelection(first.content, start, selectedRows.length === 1 ? end : start);
  const chapter = (material.chapters || []).find((item) => startPosition >= Number(item.start_paragraph) && startPosition <= Number(item.end_paragraph));
  const segments = selectedRows.map((row, index) => {
    const segmentStart = index === 0 ? start : 0;
    const segmentEnd = index === selectedRows.length - 1 ? end : row.content.length;
    return { segment_id: row.stable_anchor || `p-${row.position}`, paragraph_id: row.id || row.stable_anchor || `p-${row.position}`, paragraph_position: row.position, start_offset: segmentStart, end_offset: segmentEnd, selected_text: row.content.slice(segmentStart, segmentEnd) };
  });
  return {
    document_id: material.id, document_version: material.version || 1,
    material_id: material.id, material_version: material.version || 1,
    chapter_id: chapter?.id ?? chapter?.position ?? null,
    segment_id: segments[0].segment_id, paragraph_id: segments[0].paragraph_id,
    paragraph_position: startPosition, paragraph_end: endPosition, start_offset: start, end_offset: end,
    selected_text: quote, quote, paragraph_text: first.content,
    containing_sentence: containingSentence.text, containing_paragraph: first.content,
    segments, paragraphs: selectedRows,
  };
}

export function translationCacheIdentity({ documentId, documentVersion, documentHash, paragraphPosition, scope, text, providerId, providerModel, sentenceIndex = null, start = null, end = null, paragraphEnd = null }) {
  const location = scope === "sentence" ? `sentence:${sentenceIndex}` : scope === "word" ? `word:${start}-${end}` : scope === "selection" ? `selection:${paragraphPosition}-${paragraphEnd}:${start}-${end}` : `paragraph:${paragraphPosition}`;
  return [documentId || "demo", documentVersion || 1, documentHash || "demo", paragraphPosition, location, scope, "en", "zh-Hans", providerId, providerModel, text].join("|");
}

export function buildTranslationRequest({ material, paragraph, provider, target, scope, force = false }) {
  const baseSegment = paragraph.stable_anchor || "p-" + paragraph.position;
  const segmentId = scope === "sentence"
    ? baseSegment + ":sentence:" + target.sentenceIndex
    : scope === "word"
      ? baseSegment + ":word:" + target.start + "-" + target.end
      : scope === "selection"
        ? baseSegment + ":selection:" + (target.paragraphEnd ?? paragraph.position) + ":" + target.start + "-" + target.end
      : baseSegment;
  return {
    document_id: material.id,
    paragraph_position: paragraph.position,
    paragraph_end: Number.isInteger(target.paragraphEnd) ? target.paragraphEnd : paragraph.position,
    segment_id: segmentId,
    sentence_index: scope === "sentence" ? target.sentenceIndex : null,
    selection_start: Number.isInteger(target.start) ? target.start : null,
    selection_end: Number.isInteger(target.end) ? target.end : null,
    source_language: "en", target_language: "zh-Hans", provider: provider.id,
    provider_model: provider.model, translation_mode: scope,
    source_text: String(target.text || "").trim(), context_text: paragraph.content,
    translated_text: "", translated_at: "", context_translation: "", contextual_meaning: "", context_explanation: "", dictionary: [], force,
  };
}

export function buildInterpretationRequest({ material, selection, target, scope, provider = null, force = false }) {
  return {
    action: "interpret", provider, scope, document_id: material.id, document_version: material.version || 1,
    paragraph_start: Number(target.paragraphStart ?? selection?.paragraph_position ?? 0),
    paragraph_end: Number(target.paragraphEnd ?? selection?.paragraph_end ?? selection?.paragraph_position ?? 0),
    selection_start: Number.isInteger(target.start) ? target.start : null,
    selection_end: Number.isInteger(target.end) ? target.end : null,
    source_text: String(target.text || "").trim(), context_text: String(target.contextText || "").trim(),
    target_language: "zh-CN", coverage_complete: target.coverageComplete !== false,
    coverage_label: target.coverageLabel || "完整范围", force,
  };
}

export function isSingleEnglishWord(value) {
  return /^[A-Za-z]+(?:[-'][A-Za-z]+)*$/.test(String(value || "").trim());
}

export function markWordInContext(word, context) {
  const value = String(word || "").trim();
  const source = String(context || "").trim();
  if (!value || !source) return source;
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return source.replace(new RegExp(`\\b${escaped}\\b`, "i"), (match) => `⟦${match}⟧`);
}

export function contextualMeaningFromMarkedTranslation(value) {
  const match = String(value || "").match(/⟦\s*([^⟦⟧]+?)\s*⟧/);
  return match ? match[1].trim() : "";
}

export class BrowserLocalTranslationProvider {
  constructor(scope = window) {
    this.scope = scope;
    this.id = "browser_local";
    this.model = "chrome-built-in-translator";
    this.instance = null;
    this.pending = null;
  }
  async ready() {
    if (this.instance) return this.instance;
    if (this.pending) return this.pending;
    this.pending = (async () => {
      const modern = this.scope.Translator;
      if (modern && typeof modern.create === "function") {
        if (typeof modern.availability === "function") {
          const availability = await modern.availability({ sourceLanguage: "en", targetLanguage: "zh" });
          if (["unavailable", "no"].includes(availability)) throw new Error("本机尚不支持英中翻译。请使用支持 Translator API 的最新版 Chrome，并允许下载本地语言包。");
        }
        return modern.create({ sourceLanguage: "en", targetLanguage: "zh" });
      }
      const legacy = this.scope.translation;
      if (legacy && typeof legacy.createTranslator === "function") return legacy.createTranslator({ sourceLanguage: "en", targetLanguage: "zh" });
      throw new Error("当前浏览器没有 Chrome 内置 Translator API；可切换已配置的 Azure Provider。");
    })();
    try { this.instance = await this.pending; return this.instance; }
    catch (error) { this.pending = null; throw error; }
  }
  async translate(text) {
    const translator = await this.ready();
    const translated = String(await translator.translate(text)).trim();
    if (!translated) throw new Error("本地翻译没有返回结果。");
    return { translated_text: translated, dictionary: [], provider: this.id, provider_model: this.model, translated_at: new Date().toISOString(), cache_hit: false };
  }
  async lookupWord(word, context) {
    const wordResult = await this.translate(word);
    const markedContext = markWordInContext(word, context);
    const contextual = markedContext && markedContext.trim() !== word.trim() ? await this.translate(markedContext) : wordResult;
    const contextualMeaning = contextualMeaningFromMarkedTranslation(contextual.translated_text);
    return {
      ...wordResult,
      contextual_only: true,
      context_translation: contextual.translated_text,
      contextual_meaning: contextualMeaning || "语境不足，无法确定唯一含义",
      context_explanation: "",
      dictionary: [],
    };
  }
}

export class AzureTranslationProvider {
  constructor(api) { this.api = api; this.id = "azure_translator"; this.model = "translator-text-v3"; }
  async translate(payload, lookup = false) {
    return this.api("/leap/translation/" + (lookup ? "lookup" : "translate"), { method: "POST", body: JSON.stringify({ ...payload, provider: this.id, provider_model: this.model }) });
  }
  async lookupWord(payload) { return this.translate(payload, true); }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function action(label, fn, className = "") {
  const node = el("button", className, label);
  node.type = "button";
  node.addEventListener("click", fn);
  return node;
}
function card(title, meta = "", body = "") {
  const node = el("article", "domain-record-card");
  const head = el("div"); head.append(el("strong", "", title), el("small", "", meta));
  node.append(head, el("p", "", body));
  return node;
}
function blank(text) { const node = el("div", "domain-empty"); node.append(el("p", "", text)); return node; }
function list(host, items, render, emptyText) {
  host.replaceChildren();
  if (!items || !items.length) return host.append(blank(emptyText));
  items.forEach((item) => host.append(render(item)));
}
function opt(value, label) { const node = el("option", "", label); node.value = value; return node; }
function choices(select, items, label, placeholder = "请选择") {
  if (!select) return;
  const current = select.value; select.replaceChildren(opt("", placeholder));
  items.forEach((item) => select.append(opt(item.id, label(item))));
  if ([...select.options].some((item) => item.value === current)) select.value = current;
}
function tab(dialog, name) {
  dialog.querySelectorAll("[data-domain-tab]").forEach((item) => item.classList.toggle("active", item.dataset.domainTab === name));
  dialog.querySelectorAll("[data-domain-panel]").forEach((item) => item.hidden = item.dataset.domainPanel !== name);
}
function detail(label, value) {
  const node = el("div", "detail-row"); node.append(el("small", "", label), el("span", "", value == null ? "—" : String(value))); return node;
}

export function initProductDomains({ api, toast }) {
  const leapDialog = $("leap-domain-dialog");
  const pulseDialog = $("pulse-domain-dialog");
  let authGeneration = 0;
  const leap = { mode: "real", materials: [], excerpts: [], notes: [], wormholes: [], clashes: [], timeline: [], universe: { nodes: [], edges: [] }, library: [], libraryImports: [], libraryLoaded: false, activeMaterial: null, selection: null, readerOffset: 0, readerParagraphs: [], translationMode: "original", translationProvider: "browser_local", translationProviders: {}, providerMetadata: [], translationCache: new Map(), translationGeneration: 0, cloudConsent: new Set(), cloudProviderFailed: false, lastTranslation: null, currentTranslationScope: null, assistantAction: "translate", currentAssistantScope: null, assistantResults: new Map(), assistantLast: {}, assistantStates: { translate: { status: "idle", error: "" }, interpret: { status: "idle", error: "" } }, assistantGeneration: 0, assistantController: null, interpretationCapability: null, interpretationProvider: "auto", interpretationConsent: new Set(), translationSession: { browser_local: 0, azure_translator: 0, cacheSaved: 0 } };
  const pulse = { mode: "real", currency: "AUD", customers: [], skus: [], assets: [], orders: [], payments: [], inspections: [], expenses: [], selectedOrder: null };

  function status(id, text, tone = "") { const node = $(id); node.textContent = text; node.dataset.tone = tone; }
  function leapDemo() { return leap.mode === "demo"; }
  function pulseDemo() { return pulse.mode === "demo"; }
  function materialTitle(id) { const found = leap.materials.find((item) => item.id === id); return found ? found.title : "未知材料"; }

  function applyLeapDemo(data) {
    leap.materials = data.materials || []; leap.excerpts = data.excerpts || []; leap.notes = data.notes || [];
    leap.wormholes = data.wormholes || []; leap.clashes = data.clashes || []; leap.timeline = data.timeline || [];
    leap.universe = data.universe || { nodes: [], edges: [] };
    leap.home = {
      reading: leap.materials, recent_excerpts: leap.excerpts.slice(-6).reverse(), recent_notes: leap.notes.slice(-6).reverse(),
      recent_wormholes: leap.wormholes.slice(-4).reverse(), recent_clashes: leap.clashes.slice(-3).reverse(),
      next_action: "选择一份材料，选取证据并建立连接",
    };
  }
  async function leapAction(kind, payload) {
    const data = await api("/leap/demo/action", { method: "POST", body: JSON.stringify({ action: kind, payload }) });
    applyLeapDemo(data); renderLeap(); return data;
  }
  async function loadLeap() {
    const generation = authGeneration;
    status("leap-status", leapDemo() ? "正在打开隔离的跃迁域 Demo…" : "正在读取你的知识空间…");
    if (leapDemo()) {
      let data = await api("/leap/demo");
      if (!data.loaded) data = await api("/leap/demo/load", { method: "POST" });
      if (generation !== authGeneration) return;
      applyLeapDemo(data);
    } else {
      const rows = await Promise.all([api("/leap/home"), api("/leap/materials?limit=100"), api("/leap/excerpts"), api("/leap/notes"), api("/leap/wormholes"), api("/leap/clashes"), api("/leap/timeline"), api("/leap/universe")]);
      if (generation !== authGeneration) return;
      leap.home = rows[0]; leap.materials = rows[1].items || []; leap.excerpts = rows[2]; leap.notes = rows[3]; leap.wormholes = rows[4]; leap.clashes = rows[5]; leap.timeline = rows[6]; leap.universe = rows[7];
    }
    renderLeap();
    status("leap-status", (leapDemo() ? "DEMO · " : "") + leap.materials.length + " 份材料 · " + leap.excerpts.length + " 条证据 · " + leap.wormholes.length + " 条思想连接", "ok");
  }
  function materialCard(item) {
    const count = item.paragraph_count == null ? (item.paragraphs || []).length : item.paragraph_count;
    const node = card(item.title, (item.author || item.kind || "作者未知") + " · 已读 " + (item.progress_percent || 0) + "%", count + " 段 · " + ((item.tags || [item.kind]).filter(Boolean).join(" / ") || "未添加主题"));
    node.classList.add("clickable"); node.addEventListener("click", () => openMaterial(item.id)); return node;
  }
  function wormholeCard(item) {
    const left = leap.excerpts.find((row) => row.id === item.left_excerpt_id);
    const right = leap.excerpts.find((row) => row.id === item.right_excerpt_id);
    const node = card((item.left_material || materialTitle(left && left.material_id)) + " ⇌ " + (item.right_material || materialTitle(right && right.material_id)), item.relation_type, (item.left_quote || (left && left.quote) || "") + "\n↔\n" + (item.right_quote || (right && right.quote) || "") + "\n\n" + item.reflection);
    const controls = el("div", "domain-card-actions");
    if (left) controls.append(action("回到证据 A", () => jumpEvidence(left)));
    if (right) controls.append(action("回到证据 B", () => jumpEvidence(right)));
    node.append(controls); return node;
  }
  function clashCard(item) {
    const node = card(item.title, "ARGUMENT RECORD", "A · " + item.viewpoint_a + "\nB · " + item.viewpoint_b + "\n共同点 · " + (item.common_ground || "未填写") + "\n冲突点 · " + (item.disagreement || "未填写") + "\n我的判断 · " + (item.judgment || "未填写") + "\n未决问题 · " + (item.unresolved_questions || "未填写"));
    const controls = el("div", "domain-card-actions");
    const a = leap.excerpts.find((row) => row.id === item.evidence_a_excerpt_id);
    const b = leap.excerpts.find((row) => row.id === item.evidence_b_excerpt_id);
    if (a) controls.append(action("查看 A 证据", () => jumpEvidence(a)));
    if (b) controls.append(action("查看 B 证据", () => jumpEvidence(b)));
    node.append(controls); return node;
  }
  function timeline(host) {
    list(host, leap.timeline, (item) => card(item.topic, item.entries.length + " 次理解变化", item.entries.map((entry) => date(entry.created_at) + " · " + entry.content).join("\n")), "带主题保存笔记后，这里会形成认知时间轴。");
  }
  function renderUniverse(filter = "all") {
    const host = $("leap-universe"); host.replaceChildren();
    const nodes = (leap.universe.nodes || []).filter((item) => filter === "all" || item.kind === filter);
    if (!nodes.length) return host.append(blank("真实材料、主题和连接出现后，关系图会在这里生长。"));
    nodes.slice(0, 80).forEach((item, index) => {
      const node = action(item.label, () => {
        if (item.kind === "material") openMaterial(item.target_id);
        if (item.kind === "excerpt") jumpEvidence(leap.excerpts.find((row) => row.id === item.target_id));
      }, "universe-node " + item.kind);
      node.style.setProperty("--orbit", String(index % 7)); host.append(node);
    });
    host.append(el("p", "universe-edge-summary", nodes.length + " 个真实节点 · " + (leap.universe.edges || []).length + " 条真实关系"));
  }
  function renderLeap() {
    $("leap-workspace-badge").textContent = leapDemo() ? "DEMO · 演示数据" : "我的真实空间";
    $("leap-demo-banner").classList.toggle("hidden", !leapDemo());
    $("leap-import-drawer").classList.toggle("hidden", leapDemo());
    $("leap-next-action").textContent = (leap.home && leap.home.next_action) || "添加材料，建立第一条证据。";
    list($("leap-material-list"), (leap.home && leap.home.reading) || leap.materials, materialCard, "这里还没有真实材料。展开“添加自己的材料”，或进入演示。");
    list($("leap-reader-materials"), leap.materials, materialCard, "尚无材料。");
    const evidence = [...((leap.home && leap.home.recent_excerpts) || []), ...((leap.home && leap.home.recent_notes) || [])].slice(0, 6);
    list($("leap-home-evidence"), evidence, (item) => {
      const node = card(item.topic || item.material_title || materialTitle(item.material_id), item.quote ? "原文摘录" : "我的理解", item.quote || item.content);
      if (item.material_id) { node.classList.add("clickable"); node.addEventListener("click", () => jumpEvidence(item)); } return node;
    }, "建立摘录后，首页会显示最近证据。");
    list($("leap-home-wormholes"), (leap.home && leap.home.recent_wormholes) || leap.wormholes.slice(0, 4), wormholeCard, "连接两条证据后，思想关系会出现在这里。");
    list($("leap-home-clashes"), (leap.home && leap.home.recent_clashes) || leap.clashes.slice(0, 3), clashCard, "用两组证据形成第一张思想对撞记录。");
    timeline($("leap-home-timeline")); timeline($("leap-timeline"));
    const currentEvidence = leap.activeMaterial ? leap.excerpts.filter((item) => item.material_id === leap.activeMaterial.id) : leap.excerpts.slice(0, 8);
    list($("leap-excerpt-list"), currentEvidence, (item) => { const node = card(item.material_title || materialTitle(item.material_id), "段落 " + (Number(item.paragraph_position) + 1), item.quote); node.classList.add("clickable"); node.addEventListener("click", () => jumpEvidence(item)); return node; }, "选中正文并保存后，证据会出现在这里。");
    list($("leap-wormhole-list"), leap.wormholes, wormholeCard, "至少保存两条摘录，再建立一条可回溯的思想虫洞。");
    list($("leap-clash-list"), leap.clashes, clashCard, "选择两侧观点与证据，完成第一张思想对撞记录。");
    [["leap-wormhole-left", "选择证据A"], ["leap-wormhole-right", "选择证据B"], ["leap-clash-a", "选择A的证据"], ["leap-clash-b", "选择B的证据"]].forEach((pair) => choices($(pair[0]), leap.excerpts, (item) => (item.material_title || materialTitle(item.material_id)) + " · " + item.quote.slice(0, 42), pair[1]));
    renderUniverse();
  }
  function libraryLabel(provider) {
    return { gutenberg: "Project Gutenberg", standard_ebooks: "Standard Ebooks", ctext: "Chinese Text Project" }[provider] || provider;
  }
  function renderLibrary() {
    list($("leap-library-results"), leap.library, (item) => {
      const node = card(item.title, item.author + " · " + (item.language || "语言未知"), (item.edition || "版本信息未提供") + "\n" + item.licensing_note);
      node.classList.add("library-book-card");
      const meta = el("div", "library-book-meta");
      meta.append(el("span", "source-chip", libraryLabel(item.provider)), el("span", "source-chip", item.format || "TEXT"));
      meta.append(el("span", item.rights_status === "auto_import" ? "rights-chip safe" : "rights-chip review", item.rights_status === "auto_import" ? "可自动导入" : "需要人工确认"));
      const controls = el("div", "domain-card-actions");
      const source = el("a", "", "查看来源"); source.href = item.source_url; source.target = "_blank"; source.rel = "noopener noreferrer"; controls.append(source);
      if (item.rights_status === "auto_import") controls.append(action("加入跃迁域", () => importLibraryBook(item), "domain-primary"));
      else { const disabled = action("需要人工确认", () => {}); disabled.disabled = true; controls.append(disabled); }
      node.prepend(meta); node.append(controls); return node;
    }, "没有找到符合当前来源和关键词的书目。");
    list($("leap-library-imports"), leap.libraryImports, (item) => {
      const node = card(item.title, libraryLabel(item.provider) + " · " + item.status, item.status === "failed" ? item.error : (item.source_version ? "版本 " + item.source_version + " · SHA-256 " + item.source_hash.slice(0, 12) : "进度 " + item.progress + "%"));
      const progress = el("progress", "library-progress"); progress.max = 100; progress.value = item.progress || 0; node.append(progress);
      if (item.material_id) node.append(action("打开阅读器", async () => { leap.mode = "real"; await loadLeap(); await openMaterial(item.material_id); }, "domain-primary"));
      return node;
    }, "导入任务将在这里显示进度、版本和哈希。");
  }
  async function loadLibrary(query = "", provider = "all") {
    $("leap-library-status").textContent = query ? "正在从可信公开书目中搜索…" : "正在载入首批种子书目…";
    const [catalog, imports] = await Promise.all([
      api("/leap/library/search?q=" + encodeURIComponent(query) + "&provider=" + encodeURIComponent(provider), { timeoutMs: 45000 }),
      api("/leap/library/imports"),
    ]);
    leap.library = catalog.items || []; leap.libraryImports = imports || []; leap.libraryLoaded = true; renderLibrary();
    const unavailable = (catalog.provider_errors || []).map((item) => libraryLabel(item.provider)).join("、");
    $("leap-library-status").textContent = leap.library.length + " 个版本 · " + catalog.policy + (unavailable ? " · 暂不可用：" + unavailable : "");
  }
  async function importLibraryBook(item) {
    try {
      $("leap-library-status").textContent = "正在创建“" + item.title + "”导入任务…";
      const query = "provider=" + encodeURIComponent(item.provider) + "&source_item_id=" + encodeURIComponent(item.source_item_id);
      const run = await api("/leap/library/imports?" + query, { method: "POST", timeoutMs: 45000 });
      if (run.status === "duplicate") {
        leap.mode = "real"; await loadLeap(); await openMaterial(run.material_id); toast("同一来源版本已经存在，已直接打开。 "); return;
      }
      let current = run;
      for (let attempt = 0; attempt < 120 && !["success", "failed", "duplicate"].includes(current.status); attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2000));
        current = await api("/leap/library/imports/" + run.id);
        const old = leap.libraryImports.findIndex((entry) => entry.id === current.id);
        if (old >= 0) leap.libraryImports[old] = current; else leap.libraryImports.unshift(current);
        renderLibrary(); $("leap-library-status").textContent = current.title + " · " + current.status + " · " + current.progress + "%";
      }
      if (current.status === "success" || current.status === "duplicate") {
        leap.mode = "real"; await loadLeap(); await openMaterial(current.material_id); toast("公版书已导入并进入阅读器。 ");
      } else if (current.status === "failed") throw new Error(current.error || "书籍导入失败。 ");
    } catch (error) { $("leap-library-status").textContent = error.message; }
  }
  function activeProvider() {
    return leap.translationProviders[leap.translationProvider];
  }
  function translationKey(text, scope, position = "selection", scopeMeta = {}) {
    const material = leap.activeMaterial || {};
    const provider = activeProvider() || { id: leap.translationProvider, model: "unknown" };
    return translationCacheIdentity({
      documentId: material.id, documentVersion: material.version, documentHash: material.content_hash,
      paragraphPosition: position, scope, text, providerId: provider.id, providerModel: provider.model,
      sentenceIndex: scopeMeta.sentenceIndex, start: scopeMeta.start, end: scopeMeta.end, paragraphEnd: scopeMeta.paragraphEnd,
    });
  }
  function rememberTranslation(key, value) {
    if (leap.translationCache.has(key)) leap.translationCache.delete(key);
    leap.translationCache.set(key, value);
    while (leap.translationCache.size > MAX_TRANSLATION_CACHE) leap.translationCache.delete(leap.translationCache.keys().next().value);
  }
  function translationPayload(text, scope, paragraph, force = false, scopeMeta = {}) {
    const provider = activeProvider();
    return buildTranslationRequest({
      material: leap.activeMaterial, paragraph, provider,
      target: { ...scopeMeta, text }, scope, force,
    });
  }
  async function ensureCloudConsent() {
    if (leap.translationProvider !== "azure_translator") return;
    const material = leap.activeMaterial || {};
    if (material.library_source || leap.cloudConsent.has(material.id)) return;
    const accepted = window.confirm("选中的文本将发送到第三方翻译服务 Microsoft Azure Translator。不会自动上传整本文档；只有你明确请求的当前选区或当前阅读窗口会发送。是否继续？");
    if (!accepted) throw new Error("已取消云端翻译；你可以切换到本机模式。");
    leap.cloudConsent.add(material.id);
  }
  async function loadTranslationCapabilities() {
    leap.translationProviders = {
      browser_local: new BrowserLocalTranslationProvider(window),
      azure_translator: new AzureTranslationProvider(api),
    };
    try {
      const [capabilities, usage] = await Promise.all([api("/leap/translation/providers"), api("/leap/translation/stats")]);
      let interpretation;
      try { interpretation = await api("/leap/reading-assistant/capabilities"); }
      catch (_) { interpretation = { available: false, message: "内容解读能力状态暂时无法读取。" }; }
      leap.providerMetadata = capabilities.providers || [];
      leap.interpretationCapability = interpretation;
      const interpretationSelect = $("leap-interpretation-provider");
      if (interpretationSelect) {
        const providers = interpretation.providers || [];
        [...interpretationSelect.options].forEach((option) => {
          const info = providers.find((item) => item.id === option.value);
          option.disabled = !info?.configured;
          if (option.value === "auto") option.textContent = info?.configured ? "免费 · 自动" : "免费 · 自动（未配置）";
          if (option.value === "openrouter") option.textContent = info?.configured ? "免费 · OpenRouter Free" : "免费 · OpenRouter（未配置）";
          if (option.value === "gemini") option.textContent = info?.configured ? "免费 · Gemini" : "免费 · Gemini（未配置）";
          if (option.value === "openai") option.textContent = info?.configured ? "OpenAI" : "OpenAI（当前不可用）";
        });
        const preferred = providers.find((item) => item.id === leap.interpretationProvider)
          || providers.find((item) => item.id === interpretation.default_provider)
          || providers.find((item) => item.id === "auto")
          || providers.find((item) => item.id === "openrouter");
        if (preferred) leap.interpretationProvider = preferred.id;
        interpretationSelect.value = leap.interpretationProvider;
        $("leap-interpretation-provider-status").textContent = preferred
          ? `${preferred.label} · ${preferred.requested_model}${preferred.fallback_model ? ` · 不可用时转 ${preferred.fallback_model}` : ""}${preferred.free ? " · 不会自动转付费模型" : ""}`
          : interpretation.message;
      }
      const azure = leap.providerMetadata.find((item) => item.id === "azure_translator");
      const azureOption = [...$("leap-translation-provider").options].find((item) => item.value === "azure_translator");
      if (azureOption) { azureOption.disabled = !azure?.configured; azureOption.textContent = azure?.configured ? "云端 · Microsoft Azure" : "云端 · Azure（未配置）"; }
      const totals = (usage.providers || []).map((item) => item.provider + " " + Number(item.translated_characters || 0).toLocaleString() + "字").join(" · ") || "尚无翻译请求";
      const current = Object.entries(leap.translationSession).filter(([key]) => key !== "cacheSaved").map(([key, value]) => key + " " + value.toLocaleString() + "字").join(" · ");
      $("leap-translation-usage").textContent = "本次 " + current + " · 本次缓存节省 " + leap.translationSession.cacheSaved.toLocaleString() + "字 · 本月 " + usage.month + " / " + totals + " · 累计缓存节省 " + Number(usage.total_cache_hit_characters || 0).toLocaleString() + " 字";
      updateProviderCapability();
    } catch (error) { $("leap-translation-capability").textContent = "翻译能力状态暂时无法读取：" + error.message; }
  }
  function updateProviderCapability() {
    const info = leap.providerMetadata.find((item) => item.id === leap.translationProvider);
    if (leap.translationProvider === "browser_local") {
      $("leap-translation-capability").textContent = "Chrome 内置 Translator · 浏览器/设备运行 · 首次可能下载语言包 · 内部模型与大小由Chrome管理";
    } else {
      $("leap-translation-capability").textContent = info?.configured ? "Azure Translator v3 · 后端调用 · 密钥不会进入浏览器" : "Azure尚未配置，当前不可用";
    }
  }
  async function hydrateWindowCache() {
    if (leapDemo() || !leap.activeMaterial) return;
    const provider = activeProvider();
    const query = new URLSearchParams({ document_id: leap.activeMaterial.id, offset: String(leap.readerOffset), limit: String(READER_PAGE_SIZE), provider: provider.id, provider_model: provider.model, target_language: "zh-Hans" });
    try {
      const data = await api("/leap/translation/cache?" + query);
      (data.items || []).forEach((item) => {
        const paragraph = leap.readerParagraphs.find((row) => Number(row.position) === Number(item.paragraph_position));
        // Window hydration only knows the full paragraph text. Selection-level
        // word/sentence entries retain a source hash but not their source text,
        // so reconstructing their browser key from the paragraph would be wrong.
        if (paragraph && ["paragraph", "chapter_window"].includes(item.translation_mode)) {
          rememberTranslation(translationKey(paragraph.content, item.translation_mode, paragraph.position), { ...item, cache_hit: true });
        }
      });
    } catch (error) { $("leap-translation-capability").textContent = "缓存暂时不可读取；翻译仍可按需进行。"; }
  }
  async function persistLocalTranslation(payload, result) {
    if (leapDemo()) return result;
    try {
      return await api("/leap/translation/cache", { method: "PUT", body: JSON.stringify({ ...payload, translated_text: result.translated_text, translated_at: result.translated_at, context_translation: result.context_translation || "", contextual_meaning: result.contextual_meaning || "", context_explanation: result.context_explanation || "", dictionary: result.dictionary || [] }) });
    } catch (error) {
      $("leap-translation-capability").textContent = "译文已生成，但持久缓存保存失败：" + error.message;
      return result;
    }
  }
  async function translateText(text, scope, paragraph, force = false, scopeMeta = {}) {
    const value = String(text || "").trim();
    if (!value) throw new Error("没有可翻译的英文内容。");
    const key = translationKey(value, scope, paragraph.position, scopeMeta);
    if (!force && leap.translationCache.has(key)) {
      leap.translationSession.cacheSaved += value.length;
      return { ...leap.translationCache.get(key), cache_hit: true };
    }
    await ensureCloudConsent();
    const provider = activeProvider();
    if (provider.id === "azure_translator" && leap.cloudProviderFailed) throw new Error("本次会话的云翻译已因失败自动暂停；切换本地模式后仍可使用缓存与本地翻译。");
    const payload = translationPayload(value, scope, paragraph, force, scopeMeta);
    let result;
    if (provider.id === "browser_local") {
      result = scope === "word" ? await provider.lookupWord(value, paragraph.content) : await provider.translate(value);
      result = await persistLocalTranslation(payload, result);
    } else {
      try { result = scope === "word" ? await provider.lookupWord(payload) : await provider.translate(payload); }
      catch (error) { leap.cloudProviderFailed = true; throw error; }
    }
    if (result.cache_hit) leap.translationSession.cacheSaved += value.length;
    else leap.translationSession[provider.id] += value.length + (scope === "word" ? paragraph.content.length : 0);
    rememberTranslation(key, result);
    return result;
  }
  function renderTranslationResult(original, result, scope) {
    const output = $("leap-selection-translation"); output.replaceChildren();
    const scopeTitle = { word: "选词翻译", sentence: "句子对照", paragraph: "段落对照", selection: "所选文字翻译" }[scope] || "范围翻译";
    output.append(el("small", "", scopeTitle + " · " + result.provider + " / " + result.provider_model + (result.cache_hit ? " · 缓存" : "")));
    output.append(el("p", "translation-original", original));
    const dictionary = result.metadata?.dictionary || result.dictionary || [];
    if (scope === "word") {
      const metadata = result.metadata || {};
      const contextualMeaning = metadata.contextual_meaning || result.contextual_meaning || "语境不足，无法确定唯一含义";
      const basicMeaning = dictionary[0]?.display_target || result.translated_text;
      output.append(el("strong", "translation-context", "本句语境义 · " + contextualMeaning));
      output.append(el("p", "translation-core", "基础词义 · " + basicMeaning));
      const explanation = metadata.context_explanation || result.context_explanation || "";
      if (explanation) output.append(el("p", "translation-limitation", explanation));
    } else output.append(el("p", "translation-result", result.translated_text));
    output.append(el("small", "translation-audit", "AI/机器翻译，仅供辅助阅读 · " + (result.translated_at ? new Date(result.translated_at).toLocaleString("zh-CN") : "刚刚")));
    output.dataset.tone = "ok";
    leap.lastTranslation = { original, translated: result.translated_text, result, scope };
    leap.assistantLast.translate = { original, result, scope };
    $("leap-copy-translation").disabled = false; $("leap-save-translation-note").disabled = false;
  }
  const assistantScopeLabel = (scope) => ({ word: "单词", sentence: "句子", paragraph: "段落", selection: "所选文字", chapter: "章节" }[scope] || "所选文字");
  function assistantSelectionKey(actionName, scope, target) {
    const material = leap.activeMaterial || {};
    const provider = actionName === "interpret" ? leap.interpretationProvider : leap.translationProvider;
    return [actionName, provider, scope, material.id, material.version, target.paragraphStart, target.paragraphEnd, target.start, target.end, target.text].join("|");
  }
  function assistantTarget(scope) {
    const selection = leap.selection;
    if (!selection) throw new Error("请先在正文中选择内容；章节处理可先点任意章内文字定位。 ");
    const paragraph = leap.readerParagraphs.find((item) => Number(item.position) === Number(selection.paragraph_position));
    if (!paragraph) throw new Error("当前原文已变化，请重新选择。 ");
    if (scope === "sentence" && Number(selection.paragraph_end) !== Number(selection.paragraph_position)) throw new Error("句子范围不能跨段；请在目标句内重新选择。 ");
    if (scope === "paragraph") return { text: paragraph.content.trim(), paragraphStart: paragraph.position, paragraphEnd: paragraph.position, start: null, end: null, contextText: paragraph.content };
    if (scope === "sentence") {
      const sentence = sentenceAtSelection(paragraph.content, selection.start_offset, selection.end_offset);
      return { text: sentence.text, sentenceIndex: sentence.index, paragraphStart: paragraph.position, paragraphEnd: paragraph.position, start: sentence.start, end: sentence.end, contextText: paragraph.content };
    }
    return { text: selection.quote, paragraphStart: selection.paragraph_position, paragraphEnd: selection.paragraph_end, start: selection.start_offset, end: selection.end_offset, contextText: Number(selection.paragraph_end) === Number(selection.paragraph_position) ? paragraph.content : "" };
  }
  function currentAssistantTarget(scope = leap.currentAssistantScope) {
    if (!scope || scope === "chapter") return null;
    try { return assistantTarget(scope); } catch (_) { return null; }
  }
  function assistantResultFor(actionName, scope = leap.currentAssistantScope) {
    const target = currentAssistantTarget(scope);
    return target ? leap.assistantResults.get(assistantSelectionKey(actionName, scope, target)) : null;
  }
  function setScopeButtons(scope) {
    const buttons = {
      word: $("leap-translate-word"), sentence: $("leap-translate-sentence"), paragraph: $("leap-translate-paragraph"),
      selection: $("leap-assistant-selection"), chapter: $("leap-assistant-chapter"),
    };
    Object.entries(buttons).forEach(([name, button]) => {
      button.classList.toggle("active", name === scope);
      button.setAttribute("aria-pressed", String(name === scope));
    });
  }
  function renderAssistantIdle(actionName = leap.assistantAction) {
    const output = $("leap-selection-translation");
    const cached = assistantResultFor(actionName);
    if (cached) {
      const target = currentAssistantTarget();
      if (actionName === "translate") renderTranslationResult(target.text, cached, leap.currentAssistantScope);
      else renderInterpretationResult(target.text, cached, leap.currentAssistantScope);
      return;
    }
    const scopeLabel = assistantScopeLabel(leap.currentAssistantScope);
    output.textContent = leap.selection
      ? `已选择“${leap.selection.quote}” · 当前范围：${scopeLabel}。点击“${actionName === "translate" ? "翻译" : "含义"}”开始处理。`
      : leap.currentAssistantScope === "chapter" && leap.activeMaterial
        ? `当前范围：章节。点击“${actionName === "translate" ? "翻译" : "含义"}”开始处理。`
        : "请先在正文中选择文字。";
    output.dataset.tone = "";
    $("leap-copy-translation").disabled = true; $("leap-save-translation-note").disabled = true;
  }
  function selectAssistantScope(scope) {
    if (!leap.selection && scope !== "chapter") {
      const output = $("leap-selection-translation"); output.textContent = "请先在正文中选择内容。"; output.dataset.tone = "error"; return;
    }
    if (scope === "word" && !isSingleEnglishWord(leap.selection?.quote || "")) {
      const output = $("leap-selection-translation"); output.textContent = "“单词”范围需要先选择一个英文单词。"; output.dataset.tone = "error"; return;
    }
    if (scope === "sentence" && Number(leap.selection?.paragraph_end) !== Number(leap.selection?.paragraph_position)) {
      const output = $("leap-selection-translation"); output.textContent = "跨段选区不能扩展为单句；可使用所选文字、段落或章节。"; output.dataset.tone = "error"; return;
    }
    leap.assistantGeneration += 1; leap.assistantController?.abort(); leap.assistantController = null;
    leap.currentAssistantScope = scope; leap.currentTranslationScope = scope;
    $("leap-assistant-scope").textContent = assistantScopeLabel(scope); setScopeButtons(scope);
    renderAssistantIdle();
  }
  async function chapterAssistantTarget() {
    if (!leap.activeMaterial) throw new Error("请先打开一本材料。 ");
    const position = Number(leap.selection?.paragraph_position ?? leap.readerOffset);
    const chapter = (leap.activeMaterial.chapters || []).find((item) => position >= Number(item.start_paragraph) && position <= Number(item.end_paragraph));
    if (!chapter) throw new Error("当前材料没有可定位的章节；请选择单词、句子、段落或所选文字。 ");
    const data = await api("/leap/materials/" + leap.activeMaterial.id + "/chapters/" + chapter.position + "/content", { timeoutMs: 30000 });
    return { text: data.source_text, paragraphStart: data.paragraph_start, paragraphEnd: data.paragraph_end, start: null, end: null, contextText: "", coverageComplete: data.coverage_complete, coverageLabel: data.coverage_label };
  }
  function renderInterpretationResult(original, result, scope) {
    const output = $("leap-selection-translation"); output.replaceChildren();
    const rawModel = String(result.provider_model || result.requested_model || "");
    const modelName = rawModel.split("/").pop().replace(/:free$/i, "").split("-").map((part) => {
      if (/^\d/.test(part) || part.length <= 3) return part.toUpperCase();
      return part.charAt(0).toUpperCase() + part.slice(1);
    }).join(" ");
    const providerName = result.provider === "openrouter" ? "OpenRouter" : result.provider === "gemini" ? "Gemini" : result.provider === "auto" ? "免费自动" : "OpenAI";
    const generated = result.generated_at ? new Date(result.generated_at).toLocaleString("zh-CN", { hour12: false }) : "";
    const meta = el("small", "interpretation-meta", assistantScopeLabel(scope) + "含义 · " + providerName + (modelName ? " · " + modelName : "") + (result.cache_hit ? " · 缓存" : ""));
    if (generated) meta.title = "生成时间：" + generated + "\n请求模型：" + String(result.requested_model || "") + "\n实际模型：" + rawModel;
    output.append(meta);
    output.append(el("p", "translation-original", original));
    output.append(el("p", "interpretation-result", result.result_text));
    if (result.coverage && !result.coverage.complete) output.append(el("p", "translation-limitation", result.coverage.label));
    output.dataset.tone = "ok";
    leap.lastTranslation = { original, translated: result.result_text, result, scope, action: "interpret" };
    leap.assistantLast.interpret = { original, result, scope };
    $("leap-copy-translation").disabled = false; $("leap-save-translation-note").disabled = false;
  }
  async function interpretSelection(scope, force = false, generation = leap.assistantGeneration) {
    if (leapDemo()) throw new Error("内容解读只对你账号中的真实材料开放；演示材料不会发送到模型服务。 ");
    const controller = new AbortController(); leap.assistantController = controller;
    const target = scope === "chapter" ? await chapterAssistantTarget() : assistantTarget(scope);
    const key = assistantSelectionKey("interpret", scope, target);
    if (!force && leap.assistantResults.has(key)) return renderInterpretationResult(target.text, leap.assistantResults.get(key), scope);
    if (!leap.interpretationCapability?.available) throw new Error(leap.interpretationCapability?.message || "内容解读暂不可用：冰焰AI服务未配置。 ");
    const providerInfo = (leap.interpretationCapability.providers || []).find((item) => item.id === leap.interpretationProvider);
    if (!providerInfo?.configured) throw new Error("所选内容解读引擎当前不可用，请在含义设置中切换。 ");
    const material = leap.activeMaterial || {};
    if (leap.interpretationProvider === "openrouter" && !material.library_source && !leap.interpretationConsent.has(material.id)) {
      const accepted = window.confirm("选中的文本和必要上下文将发送到第三方服务 OpenRouter。不会自动上传整本私人文档；仅在你点击“含义”时发送当前主动请求的范围。是否继续？");
      if (!accepted) throw new Error("已取消OpenRouter内容解读；你可以切换到其他已配置引擎。 ");
      leap.interpretationConsent.add(material.id);
    }
    const payload = buildInterpretationRequest({ material: leap.activeMaterial, selection: leap.selection, target, scope, provider: leap.interpretationProvider, force });
    let result;
    try {
      result = await api("/leap/reading-assistant/interpret", { method: "POST", body: JSON.stringify(payload), signal: controller.signal, timeoutMs: 90000 });
    } catch (error) {
      if (leap.interpretationProvider === "openai" && /额度|credits|quota/i.test(error.message || "")) {
        const option = [...$("leap-interpretation-provider").options].find((item) => item.value === "openai");
        if (option) option.textContent = "OpenAI（当前不可用 / 额度不足）";
      }
      throw error;
    }
    if (generation !== leap.assistantGeneration) return;
    leap.assistantResults.set(key, result); renderInterpretationResult(target.text, result, scope);
  }
  async function translateSelection(scope, force = false, generation = leap.assistantGeneration) {
    if (!leap.selection) throw new Error("请先在正文中选择英文单词、句子或段落。");
    if (scope === "sentence" && Number(leap.selection.paragraph_end) !== Number(leap.selection.paragraph_position)) throw new Error("句子范围不能跨段；请在目标句内重新选择。 ");
    const target = translationTarget(leap.selection, scope);
    const original = target.text;
    if (scope === "word" && !isSingleEnglishWord(original)) throw new Error("“翻译单词”一次只接受一个英文单词；也可以改用翻译句子。");
    const paragraph = leap.readerParagraphs.find((item) => Number(item.position) === Number(leap.selection.paragraph_position));
    if (!paragraph) throw new Error("当前原文段落已变化，请重新选择。");
    const output = $("leap-selection-translation"); output.dataset.tone = "loading"; output.textContent = "正在按需翻译…";
    const result = await translateText(original, scope, paragraph, force, target);
    if (generation !== leap.assistantGeneration) return;
    leap.currentTranslationScope = scope; leap.assistantResults.set(assistantSelectionKey("translate", scope, { ...target, paragraphStart: paragraph.position, paragraphEnd: target.paragraphEnd ?? paragraph.position }), result); renderTranslationResult(original, result, scope); loadTranslationCapabilities();
  }
  function applyReaderTranslationMode() {
    const host = $("leap-reader-pages");
    host.classList.toggle("translation-stacked", leap.translationMode === "stacked");
    host.classList.toggle("translation-side-by-side", leap.translationMode === "side_by_side");
    host.classList.toggle("translation-only", leap.translationMode === "translation_only");
  }
  async function renderBilingualPage(forcePosition = null) {
    const generation = ++leap.translationGeneration;
    if (leap.translationMode === "original") return;
    await hydrateWindowCache();
    const button = $("leap-bilingual-toggle"); let completed = 0;
    for (let index = 0; index < leap.readerParagraphs.length; index += 1) {
      if (generation !== leap.translationGeneration || leap.translationMode === "original") return;
      const paragraph = leap.readerParagraphs[index];
      if (forcePosition !== null && Number(forcePosition) !== Number(paragraph.position)) continue;
      const section = $("leap-reader-pages").querySelector('[data-position="' + paragraph.position + '"]');
      if (!section) continue;
      let translated = section.querySelector(".manuscript-translation");
      if (!translated) { translated = el("div", "manuscript-translation", "正在翻译…"); section.append(translated); }
      try {
        const result = await translateText(paragraph.content, "chapter_window", paragraph, Number(forcePosition) === Number(paragraph.position));
        translated.replaceChildren(el("p", "", result.translated_text), el("small", "", result.provider + " / " + result.provider_model + (result.cache_hit ? " · 缓存" : "") + " · 机器翻译"), action("重新翻译本段", () => renderBilingualPage(paragraph.position), "translation-retry"));
        translated.dataset.tone = "ok"; completed += 1;
      } catch (error) {
        translated.replaceChildren(el("span", "", error.message), action("重试本段", () => renderBilingualPage(paragraph.position), "translation-retry"));
        translated.dataset.tone = "error";
        if (forcePosition === null) break;
      }
      button.textContent = "当前窗口翻译 " + completed + "/" + leap.readerParagraphs.length;
      if (forcePosition !== null) break;
    }
    button.textContent = "翻译当前阅读窗口";
    loadTranslationCapabilities();
  }
  function updateReaderPager() {
    const material = leap.activeMaterial || {};
    const total = Number(material.paragraph_count) || leap.readerParagraphs.length;
    const start = total ? leap.readerOffset + 1 : 0;
    const end = Math.min(total, leap.readerOffset + leap.readerParagraphs.length);
    $("leap-reader-page").textContent = start + "–" + end + " / " + total + " 段";
    $("leap-reader-previous").disabled = leap.readerOffset <= 0;
    $("leap-reader-next").disabled = end >= total;
  }
  async function openMaterial(id, focus = null, requestedOffset = null) {
    let material; let paragraphs; let offset = 0;
    if (leapDemo()) {
      material = leap.materials.find((item) => item.id === id);
      paragraphs = (material ? material.paragraphs : []).map((content, position) => ({ content, position }));
    } else {
      offset = requestedOffset == null ? (focus == null ? 0 : Math.floor(Math.max(0, focus) / READER_PAGE_SIZE) * READER_PAGE_SIZE) : Math.max(0, requestedOffset);
      const data = await api("/leap/materials/" + id + "/paragraphs?offset=" + offset + "&limit=" + READER_PAGE_SIZE);
      material = data.material; paragraphs = data.paragraphs; offset = data.offset || offset;
    }
    if (!material) return;
    leap.activeMaterial = material; leap.readerOffset = offset; leap.readerParagraphs = paragraphs; leap.selection = null; leap.translationGeneration += 1; leap.assistantGeneration += 1; leap.assistantController?.abort(); leap.assistantController = null; leap.assistantLast = {}; leap.currentAssistantScope = null;
    $("leap-note-material").value = material.id;
    $("leap-reader-title").textContent = material.title;
    $("leap-reader-meta").textContent = (material.author || material.kind || "作者未知") + " · 共 " + (material.paragraph_count || paragraphs.length) + " 段 · 选中文字可翻译、解读或建立证据";
    $("leap-selection-quote").textContent = "在正文中拖动选择单词、句子、段落或跨段文字";
    $("leap-assistant-scope").textContent = "所选文字"; setScopeButtons(null);
    $("leap-selection-translation").textContent = "“翻译”与“含义”只在你点击后分别运行；两类结果不会串用。";
    leap.lastTranslation = null; $("leap-copy-translation").disabled = true; $("leap-save-translation-note").disabled = true;
    const source = $("leap-reader-source"); source.replaceChildren(); source.classList.toggle("hidden", !material.library_source);
    if (material.library_source) {
      const info = material.library_source; source.append(el("small", "", "PUBLIC-DOMAIN SOURCE"), el("strong", "", info.source_name), el("p", "", (info.edition || "") + (info.translator ? " · 译者 " + info.translator : "")), el("p", "", info.licensing_note));
      const link = el("a", "", "查看原始来源"); link.href = info.source_url; link.target = "_blank"; link.rel = "noopener noreferrer"; source.append(link);
    }
    const chapterHost = $("leap-reader-chapters"); chapterHost.replaceChildren();
    (material.chapters || []).forEach((chapter) => chapterHost.append(action(chapter.title, async () => { const target = $("leap-reader-pages").querySelector('[data-position="' + chapter.start_paragraph + '"]'); if (target) target.scrollIntoView({ behavior: "smooth", block: "start" }); else await openMaterial(material.id, chapter.start_paragraph); }, "chapter-link")));
    if (!(material.chapters || []).length) chapterHost.append(blank("此材料没有独立章节信息。"));
    const host = $("leap-reader-pages"); host.replaceChildren();
    paragraphs.forEach((paragraph) => {
      const section = el("section", "manuscript-paragraph"); section.dataset.position = paragraph.position; section.id = paragraph.stable_anchor || ("paragraph-" + paragraph.position);
      if (paragraph.chapter_title && (paragraph.position === 0 || paragraphs.find((row) => row.position === paragraph.position - 1)?.chapter_title !== paragraph.chapter_title)) section.append(el("h4", "manuscript-chapter", paragraph.chapter_title));
      const text = el("p", "", paragraph.content); section.append(el("small", "", String(paragraph.position + 1).padStart(2, "0")), text);
      host.append(section);
    });
    updateReaderPager();
    tab(leapDialog, "reader");
    applyReaderTranslationMode();
    if (leap.translationMode !== "original") await renderBilingualPage();
    if (focus !== null) requestAnimationFrame(() => { const target = host.querySelector('[data-position="' + focus + '"]'); if (target) { target.classList.add("evidence-focus"); target.scrollIntoView({ behavior: "smooth", block: "center" }); } });
  }
  function selectionParagraphNode(node) {
    const element = node?.nodeType === 1 ? node : node?.parentElement;
    return element?.closest?.(".manuscript-paragraph") || null;
  }
  function offsetWithin(node, container, offset) {
    const range = document.createRange(); range.selectNodeContents(node); range.setEnd(container, offset); return range.toString().length;
  }
  function captureSelection() {
    const host = $("leap-reader-pages");
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !host.contains(selected.anchorNode) || !host.contains(selected.focusNode)) return;
    const range = selected.getRangeAt(0);
    const startSection = selectionParagraphNode(range.startContainer); const endSection = selectionParagraphNode(range.endContainer);
    const startNode = startSection?.querySelector("p"); const endNode = endSection?.querySelector("p");
    if (!startSection || !endSection || !startNode || !endNode || !startNode.contains(range.startContainer) || !endNode.contains(range.endContainer)) return;
    const startPosition = Number(startSection.dataset.position); const endPosition = Number(endSection.dataset.position);
    const selectedRows = leap.readerParagraphs.filter((row) => Number(row.position) >= startPosition && Number(row.position) <= endPosition);
    if (!selectedRows.length || selectedRows.length !== endPosition - startPosition + 1) return;
    let start = offsetWithin(startNode, range.startContainer, range.startOffset);
    let end = offsetWithin(endNode, range.endContainer, range.endOffset);
    const rawQuote = selectedRows.length === 1
      ? selectedRows[0].content.slice(start, end)
      : [selectedRows[0].content.slice(start), ...selectedRows.slice(1, -1).map((row) => row.content), selectedRows.at(-1).content.slice(0, end)].join("\n\n");
    const leading = rawQuote.length - rawQuote.trimStart().length;
    const trailing = rawQuote.length - rawQuote.trimEnd().length;
    start += leading; end -= trailing;
    const quote = rawQuote.trim(); if (!quote) return;
    leap.selection = createSelectionSnapshot({ material: leap.activeMaterial, rows: selectedRows, startPosition, endPosition, start, end, quote });
    const automaticScope = classifySelectionScope(leap.selection);
    leap.currentAssistantScope = automaticScope; leap.currentTranslationScope = automaticScope;
    $("leap-selection-quote").textContent = quote;
    $("leap-assistant-scope").textContent = assistantScopeLabel(automaticScope); setScopeButtons(automaticScope);
    leap.lastTranslation = null; leap.assistantLast = {}; leap.assistantStates = { translate: { status: "idle", error: "" }, interpret: { status: "idle", error: "" } }; leap.assistantGeneration += 1; leap.assistantController?.abort(); leap.assistantController = null;
    $("leap-copy-translation").disabled = true; $("leap-save-translation-note").disabled = true;
    host.querySelectorAll(".selected").forEach((item) => item.classList.remove("selected"));
    selectedRows.forEach((row) => host.querySelector('[data-position="' + row.position + '"]')?.classList.add("selected"));
    renderAssistantIdle();
  }
  async function saveSelection() {
    if (!leap.selection) throw new Error("请先在正文中拖动选择一段文字。");
    if (leapDemo()) await leapAction("excerpt", leap.selection);
    else { await api("/leap/excerpts", { method: "POST", body: JSON.stringify(leap.selection) }); await loadLeap(); }
    const saved = leap.excerpts.find((item) => item.quote === leap.selection.quote);
    if (saved) $("leap-note-excerpt").value = saved.id;
    toast("摘录已保存，并绑定到原文位置。");
  }
  async function jumpEvidence(item) { if (item) await openMaterial(item.material_id, Number(item.paragraph_position)); }

  function showAssistantTab(actionName) {
    leap.assistantAction = actionName;
    const translationTab = $("leap-assistant-translate-tab"); const interpretationTab = $("leap-assistant-interpret-tab");
    translationTab.classList.toggle("active", actionName === "translate"); interpretationTab.classList.toggle("active", actionName === "interpret");
    translationTab.setAttribute("aria-selected", String(actionName === "translate")); interpretationTab.setAttribute("aria-selected", String(actionName === "interpret"));
  }
  async function requestAssistant(scope, force = false) {
    const actionName = leap.assistantAction; const generation = ++leap.assistantGeneration;
    leap.assistantController?.abort(); leap.assistantController = null;
    leap.currentAssistantScope = scope; $("leap-assistant-scope").textContent = assistantScopeLabel(scope); setScopeButtons(scope);
    if (scope === "word" && !isSingleEnglishWord(leap.selection?.quote || "")) throw new Error("“单词”一次只接受一个英文单词；连续文字请选择句子或所选文字。 ");
    leap.assistantStates[actionName] = { status: "loading", error: "" };
    const output = $("leap-selection-translation"); output.textContent = actionName === "translate" ? "正在按需翻译…" : "正在依据原文解读…"; output.dataset.tone = "loading";
    if (actionName === "translate") {
      if (scope === "chapter") {
        if (!leap.activeMaterial) throw new Error("请先打开一本材料。 ");
        if (leap.translationMode === "original") { leap.translationMode = "stacked"; $("leap-translation-mode").value = "stacked"; applyReaderTranslationMode(); }
        await renderBilingualPage();
        if (generation !== leap.assistantGeneration) return;
        output.textContent = "章节双语继续采用当前阅读窗口按需翻译；已翻译内容保留原文对照。"; output.dataset.tone = "ok"; leap.assistantStates.translate = { status: "success", error: "" }; return;
      }
      await translateSelection(scope, force, generation);
    } else await interpretSelection(scope, force, generation);
    if (generation === leap.assistantGeneration) leap.assistantStates[actionName] = { status: "success", error: "" };
  }
  function runAssistant(scope, force = false) {
    const actionName = leap.assistantAction;
    const pending = requestAssistant(scope, force); const generation = leap.assistantGeneration;
    pending.catch((error) => {
      if (error?.name === "AbortError" || actionName !== leap.assistantAction || generation !== leap.assistantGeneration) return;
      leap.assistantStates[actionName] = { status: "error", error: error.message };
      const output = $("leap-selection-translation"); output.textContent = error.message; output.dataset.tone = "error";
    });
  }
  $("leap-reader-pages").addEventListener("mouseup", captureSelection);
  document.addEventListener("selectionchange", captureSelection);
  $("leap-assistant-translate-tab").addEventListener("click", () => { showAssistantTab("translate"); runAssistant(leap.currentAssistantScope || "selection"); });
  $("leap-assistant-interpret-tab").addEventListener("click", () => { showAssistantTab("interpret"); runAssistant(leap.currentAssistantScope || "selection"); });
  $("leap-translate-word").addEventListener("click", () => selectAssistantScope("word"));
  $("leap-translate-sentence").addEventListener("click", () => selectAssistantScope("sentence"));
  $("leap-translate-paragraph").addEventListener("click", () => selectAssistantScope("paragraph"));
  $("leap-assistant-selection").addEventListener("click", () => selectAssistantScope("selection"));
  $("leap-assistant-chapter").addEventListener("click", () => selectAssistantScope("chapter"));
  $("leap-retranslate-selection").addEventListener("click", () => {
    const scope = leap.currentAssistantScope || (leap.selection && isSingleEnglishWord(leap.selection.quote) ? "word" : "selection");
    runAssistant(scope, true);
  });
  $("leap-copy-translation").addEventListener("click", async () => {
    if (!leap.lastTranslation) return;
    await navigator.clipboard.writeText(leap.lastTranslation.translated); toast("阅读助手结果已复制。");
  });
  $("leap-save-translation-note").addEventListener("click", async () => {
    if (!leap.lastTranslation) return;
    try {
      await saveSelection();
      const note = $("leap-note-form").querySelector("textarea");
      const interpretation = leap.lastTranslation.action === "interpret";
      note.value = (interpretation ? "[AI 内容解读，仅供辅助阅读]\n" : "[AI/机器翻译，仅供辅助阅读]\n") + leap.lastTranslation.translated + "\n\n我的理解：";
      note.focus(); toast(interpretation ? "原文已作为证据保存；内容解读已写入你的个人理解。" : "原文已作为证据保存；译文只写入你的个人理解，不会冒充作者原文。");
    } catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-translation-provider").addEventListener("change", (event) => {
    leap.translationProvider = event.target.value; leap.translationGeneration += 1; leap.lastTranslation = null; leap.cloudProviderFailed = false;
    $("leap-reader-pages").querySelectorAll(".manuscript-translation").forEach((node) => node.remove()); updateProviderCapability();
  });
  $("leap-interpretation-provider").addEventListener("change", (event) => {
    leap.interpretationProvider = event.target.value;
    leap.assistantGeneration += 1; leap.assistantController?.abort(); leap.assistantController = null;
    leap.lastTranslation = null;
    const info = (leap.interpretationCapability?.providers || []).find((item) => item.id === leap.interpretationProvider);
    $("leap-interpretation-provider-status").textContent = info
      ? `${info.label} · ${info.requested_model}${info.fallback_model ? ` · 不可用时转 ${info.fallback_model}` : ""}${info.free ? " · 不会自动转付费模型" : ""}`
      : "当前解读引擎状态未知";
    if (leap.assistantAction === "interpret") renderAssistantIdle("interpret");
  });
  $("leap-translation-mode").addEventListener("change", (event) => {
    leap.translationMode = event.target.value; leap.translationGeneration += 1; applyReaderTranslationMode();
    if (leap.translationMode === "original") $("leap-reader-pages").querySelectorAll(".manuscript-translation").forEach((node) => node.remove());
  });
  $("leap-bilingual-toggle").addEventListener("click", async () => {
    if (!leap.activeMaterial) return status("leap-status", "请先选择一份英文材料。", "error");
    if (leap.translationMode === "original") { leap.translationMode = "stacked"; $("leap-translation-mode").value = "stacked"; applyReaderTranslationMode(); }
    try { await renderBilingualPage(); } catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-reader-previous").addEventListener("click", () => leap.activeMaterial && openMaterial(leap.activeMaterial.id, null, Math.max(0, leap.readerOffset - READER_PAGE_SIZE)));
  $("leap-reader-next").addEventListener("click", () => leap.activeMaterial && openMaterial(leap.activeMaterial.id, null, leap.readerOffset + READER_PAGE_SIZE));
  $("leap-selection-save").addEventListener("click", async () => { try { await saveSelection(); } catch (error) { status("leap-status", error.message, "error"); } });
  $("leap-selection-note").addEventListener("click", async () => { try { await saveSelection(); $("leap-note-form").querySelector("textarea").focus(); } catch (error) { status("leap-status", error.message, "error"); } });
  $("leap-selection-wormhole").addEventListener("click", async () => { try { await saveSelection(); tab(leapDialog, "wormholes"); } catch (error) { status("leap-status", error.message, "error"); } });
  $("leap-selection-clash").addEventListener("click", async () => { try { await saveSelection(); tab(leapDialog, "clashes"); } catch (error) { status("leap-status", error.message, "error"); } });
  $("leap-material-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget)); data.tags = data.tags.split(",").map((item) => item.trim()).filter(Boolean);
      const saved = await api("/leap/materials", { method: "POST", body: JSON.stringify(data) });
      event.currentTarget.reset(); await loadLeap(); await openMaterial(saved.id); toast("材料已保存，现在可以直接选取证据。");
    } catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-file").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0]; if (!file) return;
    try { const data = new FormData(); data.append("file", file); const saved = await api("/leap/materials/import", { method: "POST", body: data, timeoutMs: 30000 }); event.target.value = ""; await loadLeap(); await openMaterial(saved.id); }
    catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-note-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget)); data.material_id = data.material_id || (leap.activeMaterial && leap.activeMaterial.id) || null; data.excerpt_id = data.excerpt_id || null;
      if (leapDemo()) await leapAction("note", data); else { await api("/leap/notes", { method: "POST", body: JSON.stringify(data) }); await loadLeap(); }
      event.currentTarget.reset(); toast("理解已保存，并进入认知时间轴。");
    } catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-wormhole-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { const data = Object.fromEntries(new FormData(event.currentTarget)); if (leapDemo()) await leapAction("wormhole", data); else { await api("/leap/wormholes", { method: "POST", body: JSON.stringify(data) }); await loadLeap(); } event.currentTarget.reset(); toast("思想虫洞已建立，两侧证据都可回跳。"); }
    catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-clash-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { const data = Object.fromEntries(new FormData(event.currentTarget)); data.evidence_a_excerpt_id = data.evidence_a_excerpt_id || null; data.evidence_b_excerpt_id = data.evidence_b_excerpt_id || null; if (leapDemo()) await leapAction("clash", data); else { await api("/leap/clashes", { method: "POST", body: JSON.stringify(data) }); await loadLeap(); } event.currentTarget.reset(); toast("完整思想对撞记录已保存。"); }
    catch (error) { status("leap-status", error.message, "error"); }
  });
  $("leap-search-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const query = $("leap-search-query").value.trim(); let results;
    if (leapDemo()) {
      const q = query.toLowerCase();
      results = [
        ...leap.materials.filter((item) => JSON.stringify(item).toLowerCase().includes(q)).map((item) => ({ kind: "material", id: item.id, label: item.title, context: item.author })),
        ...leap.excerpts.filter((item) => item.quote.toLowerCase().includes(q)).map((item) => ({ kind: "excerpt", material_id: item.material_id, position: item.paragraph_position, label: item.quote, context: item.material_title })),
        ...leap.notes.filter((item) => JSON.stringify(item).toLowerCase().includes(q)).map((item) => ({ kind: "note", material_id: item.material_id, label: item.content, context: item.topic })),
        ...leap.wormholes.filter((item) => item.reflection.toLowerCase().includes(q)).map((item) => ({ kind: "wormhole", label: item.reflection, context: item.relation_type })),
        ...leap.clashes.filter((item) => JSON.stringify(item).toLowerCase().includes(q)).map((item) => ({ kind: "clash", label: item.title, context: item.judgment })),
      ];
    } else results = await api("/leap/search?q=" + encodeURIComponent(query));
    list($("leap-search-results"), results, (item) => { const node = card(item.label, item.kind, item.context || ""); if (item.material_id || item.kind === "material") { node.classList.add("clickable"); node.addEventListener("click", () => openMaterial(item.material_id || item.id, item.position == null ? null : item.position)); } return node; }, "没有找到匹配内容。");
  });
  leapDialog.querySelectorAll("[data-universe-filter]").forEach((node) => node.addEventListener("click", () => renderUniverse(node.dataset.universeFilter)));
  $("leap-real-mode").addEventListener("click", async () => { leap.mode = "real"; leap.activeMaterial = null; await loadLeap(); tab(leapDialog, "home"); });
  $("leap-demo-mode").addEventListener("click", async () => { leap.mode = "demo"; leap.activeMaterial = null; await loadLeap(); tab(leapDialog, "home"); });
  $("leap-demo-reset").addEventListener("click", async () => { applyLeapDemo(await api("/leap/demo/reset", { method: "POST" })); renderLeap(); toast("跃迁域 Demo 已恢复初始状态。"); });
  $("leap-library-search").addEventListener("submit", async (event) => { event.preventDefault(); try { await loadLibrary($("leap-library-query").value.trim(), $("leap-library-provider").value); } catch (error) { $("leap-library-status").textContent = error.message; } });

  function applyPulseDemo(data) { Object.assign(pulse, data); pulse.currency = data.currency || "AUD"; }
  async function loadPulse() {
    const generation = authGeneration;
    status("pulse-status", pulseDemo() ? "正在打开隔离的 Oia Demo Company…" : "正在读取真实 Oia 经营账本…");
    if (pulseDemo()) {
      let data = await api("/pulse/demo"); if (!data.loaded) data = await api("/pulse/demo/load", { method: "POST" });
      if (generation !== authGeneration) return;
      applyPulseDemo(data);
    } else {
      const now = new Date(); const from = String(now.getFullYear()) + "-01-01"; const to = String(now.getFullYear()) + "-12-31";
      const rows = await Promise.all([api("/pulse/settings"), api("/pulse/dashboard?date_from=" + from + "&date_to=" + to), api("/pulse/customers"), api("/pulse/skus"), api("/pulse/assets"), api("/pulse/orders"), api("/pulse/payments"), api("/pulse/inspections"), api("/pulse/expenses")]);
      if (generation !== authGeneration) return;
      Object.assign(pulse, { currency: rows[0].currency, dashboard: rows[1], customers: rows[2], skus: rows[3], assets: rows[4], orders: rows[5], payments: rows[6], inspections: rows[7], expenses: rows[8], dateFrom: from, dateTo: to });
    }
    renderPulse(); status("pulse-status", (pulseDemo() ? "DEMO · " : "") + pulse.customers.length + " 位客户 · " + pulse.orders.length + " 笔订单 · " + pulse.assets.length + " 件资产", "ok");
  }
  async function pulseAction(kind, payload) {
    applyPulseDemo(await api("/pulse/demo/action", { method: "POST", body: JSON.stringify({ action: kind, payload }) })); renderPulse();
  }
  function metric(label, value, hint, fn) {
    const node = el("button", "pulse-metric"); node.type = "button"; node.append(el("small", "", label), el("strong", "", String(value)), el("p", "", hint));
    if (fn) node.addEventListener("click", fn); else node.disabled = true; return node;
  }
  function renderTrend() {
    const host = $("pulse-trend"); host.replaceChildren(); const rows = (pulse.dashboard && pulse.dashboard.trends) || [];
    if (!rows.length) return host.append(blank("有跨期交易后显示趋势。"));
    const max = Math.max(1, ...rows.map((item) => item.revenue));
    rows.forEach((item) => {
      const node = action("", () => { tab(pulseDialog, "analytics"); loadPulseAnalytics(); }, "trend-bar");
      node.style.setProperty("--height", Math.max(8, item.revenue * 100 / max) + "%");
      node.append(el("i"), el("span", "", money(item.revenue, pulse.currency)), el("small", "", item.period + " · " + item.orders + "单")); host.append(node);
    });
  }
  function pulseChoices() {
    choices($("pulse-asset-sku"), pulse.skus, (item) => item.name + " · " + money(item.current_price_cents, pulse.currency));
    choices($("pulse-order-customer"), pulse.customers, (item) => item.name);
    choices($("pulse-order-sku"), pulse.skus, (item) => item.name + " · " + (item.size || "—"));
    choices($("pulse-order-asset"), pulse.assets.filter((item) => item.status === "available"), (item) => item.asset_code + " · " + ((pulse.skus.find((sku) => sku.id === item.sku_id) || {}).name || ""), "选择可用资产");
  }
  function renderJourney(order) {
    const steps = [
      ["客户", Boolean(order)], ["预约并分配资产", Boolean(order)],
      ["租金收款", order && pulse.payments.some((item) => item.order_id === order.id && item.payment_type === "rental")],
      ["押金收款", order && pulse.payments.some((item) => item.order_id === order.id && item.payment_type === "deposit")],
      ["交付", order && ["rented", "returned", "completed"].includes(order.status)],
      ["归还与检查", order && (["returned", "completed"].includes(order.status) || pulse.inspections.some((item) => item.order_id === order.id))],
      ["清洗", order && pulse.expenses.some((item) => item.order_id === order.id && item.category === "cleaning")],
      ["退押金并完成", order && order.status === "completed"],
    ];
    const host = $("pulse-journey"); host.replaceChildren();
    steps.forEach((row, index) => { const node = el("li", row[1] ? "done" : "", String(index + 1) + ". " + row[0]); host.append(node); });
  }
  function renderPulse() {
    $("pulse-workspace-badge").textContent = pulseDemo() ? "DEMO DATA · 隔离公司" : "真实 Oia 数据";
    $("pulse-demo-banner").classList.toggle("hidden", !pulseDemo());
    const m = (pulse.dashboard && pulse.dashboard.metrics) || {};
    const metrics = [
      ["Revenue", money(m.revenue, pulse.currency), "已过账租赁收入，不含押金", () => tab(pulseDialog, "finance")],
      ["Orders", m.orders || 0, "有效租赁订单", () => tab(pulseDialog, "transaction")],
      ["Cash In", money(m.cash_in, pulse.currency), "租金与押金实收", () => tab(pulseDialog, "finance")],
      ["Cash Out", money(m.cash_out, pulse.currency), "退款与已付经营费用", () => tab(pulseDialog, "finance")],
      ["Operating Expenses", money(m.operating_expenses, pulse.currency), "包含非现金折旧", () => tab(pulseDialog, "finance")],
      ["Operating Profit", money(m.operating_profit, pulse.currency), "收入减已记录费用", () => tab(pulseDialog, "finance")],
      ["Deposits Held", money(m.deposits_held, pulse.currency), "负债，不计入收入", () => tab(pulseDialog, "transaction")],
      ["Receivables", money(m.outstanding_receivables || 0, pulse.currency), "未收应收款", () => tab(pulseDialog, "finance")],
      ["Asset Utilization", String(m.asset_utilization == null ? (m.asset_utilization_proxy || 0) : m.asset_utilization) + "%", "非可用资产 / 全部资产", () => tab(pulseDialog, "assets")],
      ["Available Assets", m.available_assets == null ? pulse.assets.filter((item) => item.status === "available").length : m.available_assets, "当前可预约实物", () => tab(pulseDialog, "assets")],
      ["Average Order Value", money(m.average_order_value, pulse.currency), "有效订单成交额均值", () => tab(pulseDialog, "transaction")],
      ["Repeat Rate", String(m.repeat_customer_rate || 0) + "%", "两笔及以上订单客户", () => tab(pulseDialog, "analytics")],
    ];
    $("pulse-metrics").replaceChildren(...metrics.map((item) => metric(...item))); renderTrend();
    const groups = {}; pulse.assets.forEach((item) => { groups[item.status] = (groups[item.status] || 0) + 1; });
    $("pulse-asset-status").replaceChildren(...Object.entries(groups).map((row) => card(row[0], row[1] + " 件", Math.round(row[1] * 100 / Math.max(1, pulse.assets.length)) + "%")));
    list($("pulse-exceptions"), (pulse.dashboard && pulse.dashboard.recent_exceptions) || [], (item) => card(item.kind, item.count + " 条", item.detail), "当前没有需要处理的异常。");
    list($("pulse-order-list"), pulse.orders, (item) => {
      const customer = pulse.customers.find((row) => row.id === item.customer_id);
      const node = card("#" + item.id.slice(-8) + " · " + ((customer && customer.name) || "未知客户"), item.status + " · " + date(item.start_at) + " → " + date(item.end_at), money(item.total_cents, item.currency || pulse.currency) + " · 点击查看完整证据链");
      node.classList.add("clickable"); node.addEventListener("click", () => openOrder(item.id)); return node;
    }, "创建客户和订单后，交易工作台会显示完整业务链。");
    list($("pulse-asset-list"), pulse.assets, (item) => {
      const sku = pulse.skus.find((row) => row.id === item.sku_id);
      const node = card(item.asset_code, item.status + " · " + ((sku && sku.size) || "尺寸未记"), ((sku && sku.name) || "未知SKU") + " · 采购成本 " + money(item.purchase_cost_cents, pulse.currency));
      node.classList.add("clickable"); node.addEventListener("click", () => openAsset(item.id)); return node;
    }, "先创建SKU并登记实物资产。");
    pulseChoices(); renderJourney(pulse.selectedOrder);
    $("pulse-sku-form").closest("details").classList.toggle("hidden", pulseDemo());
    $("pulse-asset-form").classList.toggle("hidden", pulseDemo());
  }
  async function openOrder(id) {
    const order = pulseDemo() ? await api("/pulse/demo/orders/" + id) : await api("/pulse/orders/" + id);
    pulse.selectedOrder = order; renderJourney(order);
    const host = $("pulse-order-detail"); host.replaceChildren();
    const head = el("header"); head.append(el("small", "", "ORDER LIFECYCLE"), el("h3", "", "#" + order.id.slice(-8)), el("p", "", order.customer.name + " · " + order.status)); host.append(head);
    [["Rental Period", date(order.start_at) + " → " + date(order.end_at)], ["Amount", money(order.total_cents, order.currency || pulse.currency)], ["Channel", order.channel || "—"], ["Assigned Asset", order.items.map((item) => item.asset_code || "未分配").join(", ")]].forEach((row) => host.append(detail(row[0], row[1])));
    const controls = el("div", "detail-actions");
    [["记录租金", "payment"], ["收取押金", "deposit"], ["交付", "deliver"], ["归还", "return"], ["归还检查", "inspect"], ["记录清洗", "cleaning"], ["退还押金", "refund"]].forEach((row) => controls.append(action(row[0], () => orderAction(row[1], order))));
    host.append(controls);
    const evidence = el("section", "evidence-chain");
    evidence.append(detail("Payments", (order.payments || []).map((item) => item.payment_type + " " + money(item.amount_cents, item.currency || pulse.currency)).join("\n") || "尚无"));
    evidence.append(detail("Inspection", (order.inspections || []).map((item) => item.condition_status + " · " + item.resolution_status).join("\n") || "尚无"));
    evidence.append(detail("Expenses", (order.expenses || []).map((item) => item.category + " " + money(item.amount_cents, pulse.currency)).join("\n") || "尚无"));
    const journals = el("div", "domain-list compact-list");
    (order.journals || []).forEach((journal) => { const node = card(journal.description, journal.posting_date, journal.lines ? journal.lines.map((line) => line.account_code + " " + (line.debit_cents ? "Dr " : "Cr ") + money(line.debit_cents || line.credit_cents, pulse.currency)).join("\n") : "点击查看会计分录"); node.classList.add("clickable"); node.addEventListener("click", () => showJournal(journal)); journals.append(node); });
    evidence.append(journals); host.append(evidence);
  }
  async function orderAction(kind, order) {
    try {
      const item = order.items[0]; let cents = null;
      if (["payment", "deposit", "cleaning", "refund"].includes(kind)) {
        const fallback = kind === "payment" ? order.total_cents / 100 : kind === "cleaning" ? 50 : 150;
        const answer = window.prompt(kind + " 金额（" + pulse.currency + "）", String(fallback)); if (answer === null) return;
        cents = Math.round(Number(answer) * 100); if (!cents) throw new Error("请输入有效金额。");
      }
      if (pulseDemo()) await pulseAction(kind, { order_id: order.id, asset_id: item.asset_id, amount_cents: cents, condition_status: "cleaning_required" });
      else if (["payment", "deposit", "refund"].includes(kind)) {
        const type = kind === "payment" ? "rental" : kind === "deposit" ? "deposit" : "deposit_refund";
        await api("/pulse/payments", { method: "POST", body: JSON.stringify({ order_id: order.id, payment_type: type, amount_cents: cents, method: "Manual", reference: "" }) });
      } else if (kind === "deliver" || kind === "return") {
        await api("/pulse/orders/" + order.id + "/status", { method: "POST", body: JSON.stringify({ status: kind === "deliver" ? "rented" : "returned", notes: "脉冲域 " + kind }) });
      } else if (kind === "inspect") {
        await api("/pulse/inspections", { method: "POST", body: JSON.stringify({ order_id: order.id, asset_id: item.asset_id, condition_status: "cleaning_required", missing_items: "", damage_notes: "", resolution_status: "confirmed" }) });
      } else if (kind === "cleaning") {
        await api("/pulse/expenses", { method: "POST", body: JSON.stringify({ category: "cleaning", amount_cents: cents, status: "paid", vendor_id: null, order_id: order.id, asset_id: item.asset_id, description: "Post-rental cleaning" }) });
        await api("/pulse/assets/" + item.asset_id + "/status", { method: "POST", body: JSON.stringify({ status: "available", order_id: order.id, notes: "清洗完成，可再次出租" }) });
      }
      if (!pulseDemo() && kind === "refund") await api("/pulse/orders/" + order.id + "/status", { method: "POST", body: JSON.stringify({ status: "completed", notes: "押金已处理，订单完成" }) });
      await loadPulse(); await openOrder(order.id); toast("业务动作已完成，资产、凭证、报表和分析已同步更新。");
    } catch (error) { status("pulse-status", error.message, "error"); }
  }
  async function openAsset(id) {
    const data = pulseDemo() ? await api("/pulse/demo/assets/" + id) : await api("/pulse/assets/" + id + "/economics");
    const asset = data.asset || data; const host = $("pulse-asset-detail"); host.replaceChildren();
    const sku = pulse.skus.find((item) => item.id === asset.sku_id) || {};
    const head = el("header"); head.append(el("small", "", "DIGITAL ASSET PASSPORT"), el("h3", "", asset.asset_code), el("p", "", asset.status)); host.append(head);
    [["SKU", sku.name], ["Size", sku.size], ["Acquisition", asset.acquisition_date], ["Purchase Cost", money(asset.purchase_cost_cents, pulse.currency)], ["Lifetime Revenue", money(data.lifetime_revenue, pulse.currency)], ["Rental Count", data.rental_count], ["Direct Cost", money(data.recorded_direct_cost == null ? (data.cleaning_cost || 0) + (data.repair_cost || 0) : data.recorded_direct_cost, pulse.currency)], ["Contribution", money(data.contribution, pulse.currency)], ["Payback", String(data.payback_progress == null ? (data.roi == null ? 0 : Math.round(data.roi * 100)) : data.payback_progress) + "%"]].forEach((row) => host.append(detail(row[0], row[1])));
    const history = el("div", "domain-list compact-list");
    (data.rental_history || data.history || []).forEach((item) => { const node = card((item.id || item.event_type).slice(-8), item.status || date(item.occurred_at), item.total_cents ? money(item.total_cents, pulse.currency) : (item.notes || "")); if (item.items) node.addEventListener("click", () => { tab(pulseDialog, "transaction"); openOrder(item.id); }); history.append(node); }); host.append(history);
  }
  function showJournal(journal) {
    tab(pulseDialog, "finance"); const host = $("pulse-journal-detail"); host.replaceChildren();
    const head = el("header"); head.append(el("small", "", "JOURNAL ENTRY"), el("h3", "", journal.description), el("p", "", journal.posting_date + " · " + journal.source_document_type)); host.append(head);
    (journal.lines || []).forEach((line) => host.append(detail(line.account_code + " · " + line.account_name, (line.debit_cents ? "Debit " : "Credit ") + money(line.debit_cents || line.credit_cents, pulse.currency))));
  }
  async function loadFinance() {
    let trial; let statements; let accounts; let ledger;
    if (pulseDemo()) {
      trial = pulse.trial_balance; statements = pulse.statements; ledger = { accounts: trial.accounts.map((item) => ({ ...item, debit: item.closing_balance > 0 ? item.closing_balance : 0, credit: item.closing_balance < 0 ? -item.closing_balance : 0 })) };
      accounts = trial.accounts;
    } else {
      const rows = await Promise.all([api("/pulse/trial-balance?date_from=" + pulse.dateFrom + "&date_to=" + pulse.dateTo), api("/pulse/statements?date_from=" + pulse.dateFrom + "&date_to=" + pulse.dateTo), api("/pulse/accounts"), api("/pulse/general-ledger?date_from=" + pulse.dateFrom + "&date_to=" + pulse.dateTo)]);
      trial = rows[0]; statements = rows[1]; accounts = rows[2]; ledger = rows[3];
    }
    $("pulse-finance-output").replaceChildren(metric("Trial Balance", trial.balanced ? "Balanced" : "Integrity Error", "Debit " + money(trial.total_debit, pulse.currency) + " · Credit " + money(trial.total_credit, pulse.currency)), metric("Revenue", money(statements.income_statement.revenue_total, pulse.currency), "押金不计入收入"), metric("Operating Profit", money(statements.income_statement.operating_profit, pulse.currency), "包含非现金折旧"), metric("Balance Sheet", statements.balance_sheet.balanced ? "A = L + E" : "Opening Balance Gap", money(statements.balance_sheet.assets_total, pulse.currency) + " assets"));
    const expenses = statements.income_statement.expenses_total == null ? Object.values(statements.income_statement.expenses || {}).reduce((a, b) => a + b, 0) : statements.income_statement.expenses_total;
    const liabilities = statements.balance_sheet.liabilities_total == null ? Object.values(statements.balance_sheet.liabilities || {}).reduce((a, b) => a + b, 0) : statements.balance_sheet.liabilities_total;
    const equity = statements.balance_sheet.equity_total == null ? Object.values(statements.balance_sheet.equity || {}).reduce((a, b) => a + b, 0) : statements.balance_sheet.equity_total;
    $("pulse-statements").replaceChildren(card("Income Statement", "P&L", "Revenue " + money(statements.income_statement.revenue_total, pulse.currency) + "\nExpenses " + money(expenses, pulse.currency) + "\nProfit " + money(statements.income_statement.operating_profit, pulse.currency)), card("Balance Sheet", statements.balance_sheet.balanced ? "BALANCED" : "CHECK", "Assets " + money(statements.balance_sheet.assets_total, pulse.currency) + "\nLiabilities " + money(liabilities, pulse.currency) + "\nEquity " + money(equity, pulse.currency)), card("Cash Flow", "DIRECT METHOD", "Operating " + money(statements.cash_flow.operating, pulse.currency) + "\nInvesting " + money(statements.cash_flow.investing, pulse.currency) + "\nFinancing " + money(statements.cash_flow.financing, pulse.currency)));
    list($("pulse-ledger-list"), ledger.accounts.filter((item) => item.debit || item.credit || item.closing_balance), (item) => card(item.code + " · " + item.name, item.account_type, "Debit " + money(item.debit || 0, pulse.currency) + " · Credit " + money(item.credit || 0, pulse.currency) + " · Balance " + money(item.closing_balance || 0, pulse.currency)), "本期无总账发生额。");
    list($("pulse-account-list"), accounts, (item) => card(item.code + " · " + item.name, item.account_type, item.role || "会计科目"), "尚无会计科目。");
  }
  async function loadPulseAnalytics() {
    const data = pulseDemo() ? pulse.analytics : await api("/pulse/analytics?date_from=" + pulse.dateFrom + "&date_to=" + pulse.dateTo);
    const revenue = data.revenue_by_sku || (data.sales && data.sales.revenue_by_sku) || [];
    list($("pulse-analytics-output"), revenue, (item) => card(item.name, item.orders + " 单 · " + (item.size || ""), money(item.revenue, pulse.currency)), "尚无已确认租赁收入。");
    const summary = data.customer_summary || {};
    $("pulse-customer-summary").replaceChildren(metric("New", summary.new_customers || 0, "仅一笔订单客户"), metric("Returning", summary.returning_customers || 0, "两笔及以上客户"), metric("Average Spend", money(summary.average_spend || 0, pulse.currency), "有订单客户均值"), metric("Referral", summary.referral_customers || 0, "推荐渠道客户"));
    list($("pulse-channel-output"), summary.channel_revenue || [], (item) => card(item.channel, "ACQUISITION CHANNEL", money(item.revenue, pulse.currency)), "尚无渠道收入数据。");
    list($("pulse-cohort-output"), summary.cohorts || [], (item) => card(item.cohort + " Cohort", item.customers + " 位客户 · " + item.orders + " 单", money(item.revenue, pulse.currency)), "真实客户积累后显示 Cohort。");
    list($("pulse-segment-output"), data.customers || [], (item) => card(item.name, item.segment + " · " + item.orders + " 单", money(item.lifetime_revenue, pulse.currency) + " · Recency " + (item.recency_days == null ? "—" : item.recency_days) + "天"), "尚无客户行为数据。");
    list($("pulse-asset-analytics"), data.top_assets || [], (item) => { const node = card(item.asset_code, item.rental_count + " 次租赁 · 利用率 " + (item.utilization == null ? "—" : item.utilization + "%"), money(item.lifetime_revenue, pulse.currency) + " · 回本 " + item.payback_progress + "% · 每可用日 " + money(item.revenue_per_available_day || 0, pulse.currency)); node.classList.add("clickable"); node.addEventListener("click", () => { tab(pulseDialog, "assets"); openAsset(item.id); }); return node; }, "真实资产经营记录出现后显示资产经济性。");
    list($("pulse-insight-list"), data.insights || [], (item) => card(item.title, item.period + " · Metric " + item.metric, item.calculation + "\nEvidence: " + (item.evidence_ids || []).join(", ")), "数据不足时不编造经营洞察。");
  }

  $("pulse-customer-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try { const data = Object.fromEntries(new FormData(event.currentTarget)); if (pulseDemo()) await pulseAction("customer", data); else await api("/pulse/customers", { method: "POST", body: JSON.stringify(data) }); event.currentTarget.reset(); await loadPulse(); toast("客户已保存，可立即创建租赁订单。"); }
    catch (error) { status("pulse-status", error.message, "error"); }
  });
  $("pulse-sku-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (pulseDemo()) return;
    try { const data = Object.fromEntries(new FormData(event.currentTarget)); data.current_price_cents = Math.round(Number(data.current_price) * 100); delete data.current_price; await api("/pulse/skus", { method: "POST", body: JSON.stringify(data) }); event.currentTarget.reset(); await loadPulse(); }
    catch (error) { status("pulse-status", error.message, "error"); }
  });
  $("pulse-asset-form").addEventListener("submit", async (event) => {
    event.preventDefault(); if (pulseDemo()) return;
    try { const data = Object.fromEntries(new FormData(event.currentTarget)); data.purchase_cost_cents = data.purchase_cost ? Math.round(Number(data.purchase_cost) * 100) : null; delete data.purchase_cost; data.residual_value_cents = 0; data.useful_life_months = data.useful_life_months ? Number(data.useful_life_months) : null; data.acquisition_date = data.acquisition_date || null; await api("/pulse/assets", { method: "POST", body: JSON.stringify(data) }); event.currentTarget.reset(); await loadPulse(); }
    catch (error) { status("pulse-status", error.message, "error"); }
  });
  $("pulse-order-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const data = Object.fromEntries(new FormData(event.currentTarget)); const sku = pulse.skus.find((item) => item.id === data.sku_id);
      if (pulseDemo()) await pulseAction("order", { customer_id: data.customer_id, asset_id: data.asset_id, start_at: iso(data.start_at), end_at: iso(data.end_at), amount_cents: data.unit_price ? Math.round(Number(data.unit_price) * 100) : (sku && sku.current_price_cents), channel: data.channel });
      else await api("/pulse/orders", { method: "POST", body: JSON.stringify({ customer_id: data.customer_id, start_at: iso(data.start_at), end_at: iso(data.end_at), channel: data.channel || "", delivery_method: data.delivery_method || "", discount_cents: 0, notes: data.notes || "", items: [{ sku_id: data.sku_id, asset_id: data.asset_id, quantity: 1, unit_price_cents: data.unit_price ? Math.round(Number(data.unit_price) * 100) : null }] }) });
      event.currentTarget.reset(); await loadPulse(); const latest = pulseDemo() ? pulse.orders[pulse.orders.length - 1] : pulse.orders[0]; if (latest) await openOrder(latest.id); toast("订单已创建，具体资产已被占用。");
    } catch (error) { status("pulse-status", error.message, "error"); }
  });
  $("pulse-real-mode").addEventListener("click", async () => { pulse.mode = "real"; pulse.selectedOrder = null; await loadPulse(); tab(pulseDialog, "overview"); });
  $("pulse-demo-mode").addEventListener("click", async () => { pulse.mode = "demo"; pulse.selectedOrder = null; await loadPulse(); tab(pulseDialog, "overview"); });
  $("pulse-demo-reset").addEventListener("click", async () => { applyPulseDemo(await api("/pulse/demo/reset", { method: "POST" })); renderPulse(); toast("Oia Demo Company 已恢复到会计一致的六个月样本。"); });

  leapDialog.querySelectorAll("[data-domain-tab]").forEach((node) => node.addEventListener("click", async () => { tab(leapDialog, node.dataset.domainTab); if (node.dataset.domainTab === "library" && !leap.libraryLoaded) { try { await loadLibrary(); } catch (error) { $("leap-library-status").textContent = error.message; } } }));
  pulseDialog.querySelectorAll("[data-domain-tab]").forEach((node) => node.addEventListener("click", async () => { tab(pulseDialog, node.dataset.domainTab); if (node.dataset.domainTab === "finance") await loadFinance(); if (node.dataset.domainTab === "analytics") await loadPulseAnalytics(); }));
  $("leap-domain-close").addEventListener("click", () => leapDialog.close());
  $("pulse-domain-close").addEventListener("click", () => pulseDialog.close());
  leapDialog.addEventListener("cancel", (event) => { event.preventDefault(); leapDialog.close(); });
  pulseDialog.addEventListener("cancel", (event) => { event.preventDefault(); pulseDialog.close(); });

  return {
    async openLeap() { if (!leapDialog.open) leapDialog.showModal(); tab(leapDialog, "home"); try { await Promise.all([loadLeap(), loadTranslationCapabilities()]); } catch (error) { status("leap-status", error.message, "error"); } },
    async openPulse() { if (!pulseDialog.open) pulseDialog.showModal(); tab(pulseDialog, "overview"); try { await loadPulse(); } catch (error) { status("pulse-status", error.message, "error"); } },
    reset() {
      authGeneration += 1;
      Object.assign(leap, {
        mode: "real", materials: [], excerpts: [], notes: [], wormholes: [], clashes: [], timeline: [],
        universe: { nodes: [], edges: [] }, library: [], libraryImports: [], libraryLoaded: false,
        activeMaterial: null, selection: null, readerOffset: 0, readerParagraphs: [], home: null,
        providerMetadata: [], lastTranslation: null, currentTranslationScope: null, assistantAction: "translate",
        currentAssistantScope: null, assistantLast: {}, assistantStates: { translate: { status: "idle", error: "" }, interpret: { status: "idle", error: "" } }, interpretationCapability: null,
        translationSession: { browser_local: 0, azure_translator: 0, cacheSaved: 0 },
      });
      leap.assistantGeneration += 1;
      leap.assistantController?.abort(); leap.assistantController = null;
      leap.assistantResults.clear();
      leap.translationGeneration += 1;
      leap.translationCache.clear();
      leap.cloudConsent.clear();
      leap.interpretationConsent.clear();
      Object.assign(pulse, {
        mode: "real", currency: "AUD", customers: [], skus: [], assets: [], orders: [], payments: [],
        inspections: [], expenses: [], selectedOrder: null, dashboard: null,
      });
      if (leapDialog.open) leapDialog.close();
      if (pulseDialog.open) pulseDialog.close();
    },
  };
}
