import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  PRODUCT_NAV_ITEMS,
  normalizeProductId,
  productDialogId,
  productDialogIdsToClose,
  resolveStartupProduct,
} from "./product-navigation.js";

test("the global product navigation contains every formal product exactly once", () => {
  assert.equal(PRODUCT_NAV_ITEMS.length, 10);
  assert.equal(new Set(PRODUCT_NAV_ITEMS.map((item) => item.id)).size, 10);
  assert.deepEqual(
    PRODUCT_NAV_ITEMS.map((item) => item.label),
    ["寒冰域", "极光域", "烈火域", "未来雷达", "造界", "共振", "溯源透镜", "八度空间", "光子魅影", "遗忘史诗"],
  );
});

test("workspace products stay in the main surface and modal products resolve their dialog", () => {
  assert.equal(productDialogId("general"), null);
  assert.equal(productDialogId("recruitment"), "recruitment-dialog");
  assert.equal(productDialogId("oblivion"), "oblivion-archive-dialog");
  assert.equal(productDialogId("unknown"), null);
});

test("every modal product has a real navigation surface and all product buttons share one launcher", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
  const modalProducts = PRODUCT_NAV_ITEMS.filter((item) => item.dialogId);

  assert.equal(modalProducts.length, 7);
  modalProducts.forEach((item) => {
    assert.match(html, new RegExp(`<dialog[^>]+id=["']${item.dialogId}["']`));
  });
  assert.match(appSource, /PRODUCT_NAV_ITEMS\.filter\(\(item\) => item\.dialogId\)\.forEach/);
  assert.match(appSource, /button\.addEventListener\("click", \(\) => launchProduct\(item\.id\)\)/);
  assert.match(appSource, /worldMapButton\.addEventListener\("click", openWorldMap\)/);
  assert.match(appSource, /closeOpenProductDialogs\(product\)/);
});

test("switching products closes every previous product dialog but keeps the destination", () => {
  const switchingToTrace = productDialogIdsToClose("trace");
  assert.equal(switchingToTrace.includes("trace-dialog"), false);
  assert.equal(switchingToTrace.includes("recruitment-dialog"), true);
  assert.equal(switchingToTrace.includes("music-dimension-dialog"), true);
  assert.equal(productDialogIdsToClose("general").length, 7);
});

test("only known products can be restored or launched", () => {
  assert.equal(normalizeProductId("music"), "music");
  assert.equal(normalizeProductId("unknown"), null);
  assert.equal(resolveStartupProduct({ queuedProductLaunch: "unknown", pendingLaunch: "finance" }), "finance");
});

test("the visible Chinese world-map name changes without renaming the English product", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.match(html, /<h2>冰焰世界地图<\/h2>/);
  assert.doesNotMatch(html, /冰焰产品罗盘/);
  assert.match(html, /FROSTFIRE<br \/>PRODUCT COMPASS/);
});

test("a previously active product does not bypass the product compass on startup", () => {
  assert.equal(resolveStartupProduct({ activeProduct: "general" }), null);
  assert.equal(resolveStartupProduct({ activeProduct: "recruitment" }), null);
});

test("an explicit product choice made before authentication is resumed", () => {
  assert.equal(resolveStartupProduct({ pendingLaunch: "recruitment" }), "recruitment");
  assert.equal(
    resolveStartupProduct({ queuedProductLaunch: "music", pendingLaunch: "recruitment" }),
    "music",
  );
});
