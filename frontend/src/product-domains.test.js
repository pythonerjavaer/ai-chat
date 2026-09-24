import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
