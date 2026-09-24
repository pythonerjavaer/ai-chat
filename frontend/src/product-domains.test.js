import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { BrowserLocalTranslationProvider, isSingleEnglishWord, sentenceAroundSelection } from "./product-domains.js";

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
  for (const marker of ["翻译单词", "翻译句子", "翻译段落", "翻译当前阅读窗口", "BrowserLocalTranslationProvider", "AzureTranslationProvider", "scope.Translator", "scope.translation", "READER_PAGE_SIZE", "manuscript-translation", "/leap/translation/cache", "AI/机器翻译，仅供辅助阅读"]) {
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

test("browser-local provider uses the device translator and keeps word and context meanings separate", async () => {
  const calls = [];
  const provider = new BrowserLocalTranslationProvider({
    Translator: {
      availability: async () => "available",
      create: async () => ({ translate: async (text) => { calls.push(text); return "译：" + text; } }),
    },
  });
  const result = await provider.lookupWord("reason", "Reason guides choice.");
  assert.equal(result.translated_text, "译：reason");
  assert.equal(result.context_translation, "译：Reason guides choice.");
  assert.deepEqual(calls, ["reason", "Reason guides choice."]);
  assert.equal(result.provider, "browser_local");
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
