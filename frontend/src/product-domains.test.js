import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  BrowserLocalTranslationProvider,
  buildInterpretationRequest,
  buildTranslationRequest,
  classifySelectionScope,
  contextualMeaningFromMarkedTranslation,
  createSelectionSnapshot,
  isSingleEnglishWord,
  markWordInContext,
  sentenceAroundSelection,
  sentenceRanges,
  translationCacheIdentity,
  translationTarget,
} from "./product-domains.js";

const source = readFileSync(new URL("./product-domains.js", import.meta.url), "utf8");
const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");

test("Leap supports an evidence-backed reading journey and isolated demo", () => {
  for (const marker of ["/leap/demo/load", "window.getSelection", "leap-selection-card", "/leap/timeline", "/leap/search"]) {
    assert.match(source + html, new RegExp(marker.replaceAll("/", "\\/")));
  }
});

test("Leap public-domain library exposes trusted sources, progress and reader anchors", () => {
  for (const marker of ["公共领域书库", "/leap/library/search", "/leap/library/imports", "Project Gutenberg", "Standard Ebooks", "Chinese Text Project", "leap-reader-chapters", "stable_anchor"]) {
    assert.match(source + html, new RegExp(marker.replaceAll("/", "\\/")));
  }
  assert.match(source, /attempt\s*<\s*120/);
});

test("Leap translation providers support scoped selection and bounded bilingual reading", () => {
  for (const marker of ["leap-translate-word", "leap-translate-sentence", "leap-translate-paragraph", "翻译当前阅读窗口", "BrowserLocalTranslationProvider", "AzureTranslationProvider", "scope.Translator", "scope.translation", "READER_PAGE_SIZE", "manuscript-translation", "/leap/translation/cache", "AI/机器翻译，仅供辅助阅读"]) {
    assert.match(source + html, new RegExp(marker.replaceAll("/", "\\/")));
  }
  assert.match(html, /上下对照/);
  assert.match(html, /左右对照/);
  assert.match(html, /仅译文/);
  assert.match(source, /window\.confirm\("选中的文本将发送到第三方翻译服务/);
  assert.doesNotMatch(source, /scroll.*translate|IntersectionObserver/);
  assert.equal(isSingleEnglishWord("thought"), true);
  assert.equal(isSingleEnglishWord("well-being"), true);
  assert.equal(isSingleEnglishWord("two words"), false);
  const paragraph = "The unexamined life is not worth living. Knowledge begins in wonder!";
  const start = paragraph.indexOf("life");
  assert.equal(sentenceAroundSelection(paragraph, start, start + 4), "The unexamined life is not worth living.");
});

test("browser-local provider keeps the basic meaning separate and extracts a concise contextual sense", async () => {
  const calls = [];
  const provider = new BrowserLocalTranslationProvider({
    Translator: {
      availability: async () => "available",
      create: async () => ({ translate: async (text) => {
        calls.push(text);
        if (text.includes("⟦bank⟧")) return "他坐在⟦河岸⟧边。";
        return text === "bank" ? "银行" : "译：" + text;
      } }),
    },
  });
  const result = await provider.lookupWord("bank", "He sat by the bank.");
  assert.equal(result.translated_text, "银行");
  assert.equal(result.contextual_meaning, "河岸");
  assert.equal(result.context_translation, "他坐在⟦河岸⟧边。");
  assert.deepEqual(calls, ["bank", "He sat by the ⟦bank⟧."]);
  assert.equal(result.provider, "browser_local");
  assert.equal(markWordInContext("Bank", "The bank approved it."), "The ⟦bank⟧ approved it.");
  assert.equal(contextualMeaningFromMarkedTranslation("靠近⟦河岸⟧。"), "河岸");
});

test("sentence and paragraph translation send different source text and cache identities", () => {
  const paragraphText = "Sentence one. Sentence two is longer. Sentence three ends here.";
  const start = paragraphText.indexOf("two is");
  const selection = { paragraph_text: paragraphText, quote: "two is", start_offset: start, end_offset: start + 6 };
  const sentence = translationTarget(selection, "sentence");
  const paragraph = translationTarget(selection, "paragraph");
  assert.deepEqual(sentence, { text: "Sentence two is longer.", sentenceIndex: 1, start: 14, end: 37, paragraphEnd: undefined });
  assert.equal(paragraph.text, paragraphText);

  const shared = {
    material: { id: "doc-1", version: 3, content_hash: "hash-3" },
    paragraph: { position: 7, stable_anchor: "p-v3-000007", content: paragraphText },
    provider: { id: "browser_local", model: "chrome-built-in-translator" },
  };
  const sentenceRequest = buildTranslationRequest({ ...shared, target: sentence, scope: "sentence" });
  const paragraphRequest = buildTranslationRequest({ ...shared, target: paragraph, scope: "paragraph" });
  assert.equal(sentenceRequest.source_text, "Sentence two is longer.");
  assert.equal(sentenceRequest.translation_mode, "sentence");
  assert.equal(sentenceRequest.sentence_index, 1);
  assert.equal(sentenceRequest.segment_id, "p-v3-000007:sentence:1");
  assert.equal(paragraphRequest.source_text, paragraphText);
  assert.equal(paragraphRequest.translation_mode, "paragraph");
  assert.equal(paragraphRequest.sentence_index, null);
  assert.equal(paragraphRequest.segment_id, "p-v3-000007");

  const keyBase = { documentId: "doc-1", documentVersion: 3, documentHash: "hash-3", paragraphPosition: 7, providerId: "browser_local", providerModel: "chrome-built-in-translator" };
  const sentenceKey = translationCacheIdentity({ ...keyBase, scope: "sentence", text: sentence.text, sentenceIndex: sentence.sentenceIndex, start: sentence.start, end: sentence.end });
  const paragraphKey = translationCacheIdentity({ ...keyBase, scope: "paragraph", text: paragraph.text });
  assert.notEqual(sentenceKey, paragraphKey);
  assert.match(sentenceKey, /sentence:1\|sentence\|/);
  assert.match(paragraphKey, /paragraph:7\|paragraph\|/);
});

test("reading assistant keeps interpretation separate from translation and preserves scope", () => {
  for (const marker of ["READING ASSISTANT", "leap-assistant-translate-tab", "leap-assistant-interpret-tab", "/leap/reading-assistant/interpret", "action: \"interpret\""]) {
    assert.match(source + html, new RegExp(marker.replaceAll("/", "\\/")));
  }
  const material = { id: "doc-1", version: 4 };
  const selection = { paragraph_position: 8, paragraph_end: 8 };
  const sentenceTarget = { text: "Sentence two is longer.", paragraphStart: 8, paragraphEnd: 8, start: 14, end: 37, contextText: "Sentence one. Sentence two is longer. Sentence three." };
  const request = buildInterpretationRequest({ material, selection, target: sentenceTarget, scope: "sentence" });
  assert.deepEqual({ action: request.action, scope: request.scope, source_text: request.source_text, context_text: request.context_text }, {
    action: "interpret", scope: "sentence", source_text: "Sentence two is longer.", context_text: "Sentence one. Sentence two is longer. Sentence three.",
  });
  assert.equal(request.paragraph_start, 8);
  assert.equal(request.paragraph_end, 8);
  assert.equal(request.document_version, 4);
  assert.notEqual(JSON.stringify(request), JSON.stringify(buildTranslationRequest({
    material, paragraph: { position: 8, stable_anchor: "p-8", content: sentenceTarget.contextText },
    provider: { id: "browser_local", model: "chrome-built-in-translator" }, target: sentenceTarget, scope: "sentence",
  })));
  assert.match(source, /assistantGeneration/);
  assert.match(source, /assistantController\?\.abort/);
});

test("manual selections are classified without changing their exact offsets or text", () => {
  const paragraphText = "First sentence. The noisiest authorities objected. Final sentence.";
  const base = { paragraph_position: 4, paragraph_end: 4, paragraph_text: paragraphText };
  const wordStart = paragraphText.indexOf("noisiest");
  assert.equal(classifySelectionScope({ ...base, quote: "noisiest", start_offset: wordStart, end_offset: wordStart + 8 }), "word");

  const sentence = sentenceRanges(paragraphText)[1];
  assert.equal(classifySelectionScope({ ...base, quote: sentence.text, start_offset: sentence.start, end_offset: sentence.end }), "sentence");
  assert.equal(classifySelectionScope({ ...base, quote: paragraphText, start_offset: 0, end_offset: paragraphText.length }), "paragraph");

  const partial = { ...base, quote: "noisiest authorities", start_offset: wordStart, end_offset: wordStart + "noisiest authorities".length };
  const before = structuredClone(partial);
  assert.equal(classifySelectionScope(partial), "selection");
  assert.equal(translationTarget(partial, "selection").text, "noisiest authorities");
  assert.equal(translationTarget(partial, "sentence").text, "The noisiest authorities objected.");
  assert.equal(translationTarget(partial, "paragraph").text, paragraphText);
  assert.deepEqual(partial, before, "classification and quick scope expansion must not mutate the manual selection");
});

test("cross-paragraph snapshot retains exact multi-segment bounds and document context", () => {
  const rows = [
    { id: "seg-7", stable_anchor: "p-v2-000007", position: 7, content: "Alpha begins here." },
    { id: "seg-8", stable_anchor: "p-v2-000008", position: 8, content: "Beta finishes there." },
  ];
  const snapshot = createSelectionSnapshot({
    material: { id: "doc-9", version: 2, chapters: [{ id: "chapter-1", start_paragraph: 0, end_paragraph: 10 }] },
    rows, startPosition: 7, endPosition: 8, start: 6, end: 13, quote: "begins here.\n\nBeta finishes",
  });
  assert.equal(classifySelectionScope(snapshot), "selection");
  assert.deepEqual({ document_id: snapshot.document_id, document_version: snapshot.document_version, chapter_id: snapshot.chapter_id }, { document_id: "doc-9", document_version: 2, chapter_id: "chapter-1" });
  assert.equal(snapshot.containing_sentence, "Alpha begins here.");
  assert.equal(snapshot.containing_paragraph, rows[0].content);
  assert.deepEqual(snapshot.segments.map(({ paragraph_position, start_offset, end_offset, selected_text }) => ({ paragraph_position, start_offset, end_offset, selected_text })), [
    { paragraph_position: 7, start_offset: 6, end_offset: 18, selected_text: "begins here." },
    { paragraph_position: 8, start_offset: 0, end_offset: 13, selected_text: "Beta finishes" },
  ]);
  const target = translationTarget(snapshot, "selection");
  assert.equal(target.text, "begins here.\n\nBeta finishes");
  assert.equal(target.paragraphEnd, 8);
});

test("scope controls are request-free and actions execute the shared current scope", () => {
  const scopeHandlerMarkers = [
    '$("leap-translate-word").addEventListener("click", () => selectAssistantScope("word"))',
    '$("leap-translate-sentence").addEventListener("click", () => selectAssistantScope("sentence"))',
    '$("leap-translate-paragraph").addEventListener("click", () => selectAssistantScope("paragraph"))',
    '$("leap-assistant-selection").addEventListener("click", () => selectAssistantScope("selection"))',
    '$("leap-assistant-chapter").addEventListener("click", () => selectAssistantScope("chapter"))',
  ];
  scopeHandlerMarkers.forEach((marker) => assert.ok(source.includes(marker)));
  const scopeFunction = source.slice(source.indexOf("function selectAssistantScope"), source.indexOf("async function chapterAssistantTarget"));
  assert.doesNotMatch(scopeFunction, /\bapi\s*\(|runAssistant\s*\(|translateText\s*\(|interpretSelection\s*\(/);
  assert.match(source, /leap-assistant-translate-tab[\s\S]{0,180}runAssistant\(leap\.currentAssistantScope/);
  assert.match(source, /leap-assistant-interpret-tab[\s\S]{0,180}runAssistant\(leap\.currentAssistantScope/);
  assert.match(source, /document\.addEventListener\("selectionchange", captureSelection\)/);
  const captureFunction = source.slice(source.indexOf("function captureSelection"), source.indexOf("async function saveSelection"));
  assert.doesNotMatch(captureFunction, /\bapi\s*\(|runAssistant\s*\(|translateText\s*\(|interpretSelection\s*\(/);
  assert.match(source, /assistantSelectionKey\("translate"/);
  assert.match(source, /assistantSelectionKey\("interpret"/);
});

test("sentence boundaries preserve quotes, abbreviations, decimals and question marks", () => {
  const paragraph = 'Dr. Smith asked, "Is this clear?" The U.S. result was 3.14 times better! Final answer.';
  assert.deepEqual(sentenceRanges(paragraph).map((item) => item.text), [
    'Dr. Smith asked, "Is this clear?"',
    "The U.S. result was 3.14 times better!",
    "Final answer.",
  ]);
  const start = paragraph.indexOf("clear");
  assert.equal(sentenceAroundSelection(paragraph, start, start + 5), 'Dr. Smith asked, "Is this clear?"');
});

test("word translation UI prioritizes context and omits dictionary clutter", () => {
  assert.match(source, /本句语境义 ·/);
  assert.match(source, /基础词义 ·/);
  assert.match(source, /语境不足，无法确定唯一含义/);
  assert.doesNotMatch(source, /其他常见义项 ·/);
  assert.doesNotMatch(source, /part_of_speech \?/);
});

test("Pulse supports transaction, asset, finance and analytics drill-down", () => {
  for (const marker of ["/pulse/demo/action", "/pulse/orders/", "pulse-order-detail", "pulse-asset-detail", "pulse-journal-detail"]) {
    assert.match(source + html, new RegExp(marker.replaceAll("/", "\\/")));
  }
});

test("new products do not add hidden polling loops", () => {
  assert.doesNotMatch(source, /setInterval/);
  assert.equal((source.match(/setTimeout\s*\(/g) || []).length, 1);
});

test("only the selected workspace panel is rendered", () => {
  assert.match(css, /\.domain-panel\[hidden\]\s*\{\s*display:\s*none\s*!important/);
});
