import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import {
  PRODUCT_NAV_ITEMS,
  globalAuthCopy,
  normalizeProductId,
  productDialogId,
  productDialogIdsToClose,
  resolveSessionResumeProduct,
  resolveStartupProduct,
} from "./product-navigation.js";

test("the global product navigation contains every formal product exactly once", () => {
  assert.equal(PRODUCT_NAV_ITEMS.length, 12);
  assert.equal(new Set(PRODUCT_NAV_ITEMS.map((item) => item.id)).size, 12);
  assert.deepEqual(
    PRODUCT_NAV_ITEMS.map((item) => item.label),
    ["寒冰域", "烈火域", "极光域", "脉冲域", "跃迁域", "未来雷达", "造界", "共振", "溯源透镜", "八度空间", "光子魅影", "遗忘史诗"],
  );
});

test("both world maps use the Frost Ember Aurora Pulse Leap order", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const expected = ["legal", "finance", "general", "pulse", "leap"];
  for (const className of ["world-entry-grid", "world-map-grid"]) {
    const start = html.indexOf(className);
    const segment = html.slice(start, html.indexOf("</div>", start));
    let previous = -1;
    expected.forEach((id) => {
      const position = segment.indexOf(`data-launch="${id}"`);
      assert.ok(position > previous, `${id} should appear in the requested order in ${className}`);
      previous = position;
    });
  }
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

  assert.equal(modalProducts.length, 9);
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
  assert.equal(productDialogIdsToClose("general").length, 9);
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

test("authenticated startup opens the world map before loading a restored workspace", () => {
  const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
  const start = appSource.indexOf("async function enterApp()");
  const end = appSource.indexOf("\nasync function loadHomeRecruitmentAlerts", start);
  const enterAppSource = appSource.slice(start, end);
  const mapOpen = enterAppSource.indexOf("if (!resumeProduct) openWorldMap();");
  const firstNetworkLoad = enterAppSource.indexOf("await loadWorkspaces();");

  assert.ok(mapOpen >= 0, "normal authenticated startup must open the world map");
  assert.ok(mapOpen < firstNetworkLoad, "the map must open before the restored workspace can paint");
  assert.doesNotMatch(enterAppSource, /setTimeout\(openWorldMap/);
});

test("an explicit product choice made before authentication is resumed", () => {
  assert.equal(resolveStartupProduct({ pendingLaunch: "recruitment" }), "recruitment");
  assert.equal(
    resolveStartupProduct({ queuedProductLaunch: "music", pendingLaunch: "recruitment" }),
    "music",
  );
});

test("authentication copy always belongs to Frostfire rather than an individual product", () => {
  assert.deepEqual(globalAuthCopy("login"), {
    kicker: "FROSTFIRE / 冰焰",
    title: "登录冰焰",
    description: "一次登录，进入你的私人智能世界。",
  });
  assert.equal(globalAuthCopy("register").title, "创建冰焰账号");
  const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  assert.doesNotMatch(appSource + html, /登录后进入|注册后进入|世界入口已锁定|showPendingProductAuth/);
  assert.match(html, /id="auth-title">登录冰焰</);
});

test("session expiry resumes only a product that was actually open", () => {
  assert.equal(resolveSessionResumeProduct({ activeProduct: "leap", appVisible: true, worldMapOpen: false }), "leap");
  assert.equal(resolveSessionResumeProduct({ activeProduct: "leap", appVisible: true, worldMapOpen: false, productSurfaceOpen: false }), null);
  assert.equal(resolveSessionResumeProduct({ activeProduct: "pulse", appVisible: true, worldMapOpen: true }), null);
  assert.equal(resolveSessionResumeProduct({ activeProduct: "recruitment", appVisible: false, worldMapOpen: false }), null);
  assert.equal(resolveSessionResumeProduct({ activeProduct: "unknown", appVisible: true, worldMapOpen: false }), null);
});

test("401 uses global logout with resume while 403 remains an authorization error", () => {
  const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
  const start = appSource.indexOf("async function api(");
  const end = appSource.indexOf("\nfunction showToast", start);
  const apiSource = appSource.slice(start, end);
  assert.match(apiSource, /response\.status === 401/);
  assert.match(apiSource, /logout\(false, \{ resumeProduct, preservePending: true \}\)/);
  assert.doesNotMatch(apiSource, /response\.status === 403[^]*logout/);
});

test("Leap and Pulse use the shared Frostfire API and backend current-user dependency", () => {
  const domainsSource = readFileSync(new URL("./product-domains.js", import.meta.url), "utf8");
  const leapBackend = readFileSync(new URL("../../backend/product_domains/leap.py", import.meta.url), "utf8");
  const pulseBackend = readFileSync(new URL("../../backend/product_domains/pulse.py", import.meta.url), "utf8");
  assert.match(domainsSource, /export function initProductDomains\(\{ api, toast \}\)/);
  assert.doesNotMatch(domainsSource, /localStorage|sessionStorage|Authorization|login|password/i);
  assert.match(leapBackend, /def create_leap_router\([^)]*current_user/);
  assert.match(pulseBackend, /def create_pulse_router\([^)]*current_user/);
  assert.match(leapBackend, /Depends\(current_user\)/);
  assert.match(pulseBackend, /Depends\(current_user\)/);
});

test("global logout clears product state and closes every product dialog", () => {
  const appSource = readFileSync(new URL("./app.js", import.meta.url), "utf8");
  const start = appSource.indexOf("async function logout(");
  const end = appSource.indexOf("\nasync function loadWorkspaces", start);
  const logoutSource = appSource.slice(start, end);
  assert.match(logoutSource, /state\.activeProduct = null/);
  assert.match(logoutSource, /storage\.remove\(STORAGE_KEYS\.activeProduct\)/);
  assert.match(logoutSource, /storage\.remove\(STORAGE_KEYS\.pendingProduct\)/);
  assert.match(logoutSource, /closeOpenProductDialogs\(\)/);
  assert.match(logoutSource, /productDomains\?\.reset\(\)/);
  assert.match(logoutSource, /setAuthMode\("login"\)/);
});
