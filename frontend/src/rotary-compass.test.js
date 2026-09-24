import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

import { PRODUCT_NAV_ITEMS, normalizeProductId } from "./product-navigation.js";

const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
const setupSource = source.slice(source.indexOf("function setupRotaryCompass("), source.indexOf("function setupRotaryCompasses("));
const launchSource = source.slice(source.indexOf("async function launchProduct("), source.indexOf("async function createSpace("));

class Node {
  constructor(dataset = {}, parent = null) {
    this.dataset = dataset;
    this.parent = parent;
    this.listeners = new Map();
    this.attributes = new Map();
    this.properties = new Map();
    this.classes = new Set();
    this.capturedPointers = new Set();
    this.classList = {
      add: (value) => this.classes.add(value),
      remove: (value) => this.classes.delete(value),
      contains: (value) => this.classes.has(value),
    };
    this.style = { setProperty: (key, value) => this.properties.set(key, value) };
  }
  addEventListener(type, handler, options) {
    const records = this.listeners.get(type) || [];
    records.push({ handler, capture: options === true || options?.capture === true });
    this.listeners.set(type, records);
  }
  setAttribute(key, value) {
    this.attributes.set(key, value);
    if (key === "data-compass-selected") this.dataset.compassSelected = value;
  }
  getAttribute(key) { return this.attributes.get(key); }
  contains(node) {
    for (let current = node; current; current = current.parent) if (current === this) return true;
    return false;
  }
  closest() {
    for (let current = this; current; current = current.parent) {
      if (current.dataset.launch && current.getAttribute("aria-hidden") === "false") return current;
    }
    return null;
  }
  hasPointerCapture(id) { return this.capturedPointers.has(id); }
  setPointerCapture(id) { this.capturedPointers.add(id); }
  releasePointerCapture(id) { this.capturedPointers.delete(id); }
}

function runtime({ compact = false, id = "world", cardHeight = 138.594, token = "isolated-test-token" } = {}) {
  const container = new Node({ rotaryCompass: id });
  container.clientWidth = compact ? 340 : 980;
  const cards = PRODUCT_NAV_ITEMS.map((item) => new Node({ launch: item.id }, container));
  const dimensions = { height: cardHeight };
  cards.forEach((card) => Object.defineProperties(card, {
    offsetWidth: { get: () => compact ? (card.dataset.compassSelected === "true" ? 230 : 82) : id === "landing" ? 220 : 190 },
    offsetHeight: { get: () => compact ? (card.dataset.compassSelected === "true" ? 152 : 76) : dimensions.height },
  }));
  const children = new Map(cards.map((card) => [card.dataset.launch, new Node({}, card)]));
  container.querySelectorAll = () => cards;
  const controls = new Node();
  const left = new Node({ compassStep: "-1" }, controls);
  const right = new Node({ compassStep: "1" }, controls);
  controls.querySelectorAll = () => [left, right];
  const timers = [];
  const launchCalls = [];
  const opened = [];
  const workspaces = [];
  const storageWrites = [];
  const authModes = [];
  const toasts = [];
  const state = { token, workspace: "general", pendingLaunch: null, authMode: "login" };
  const worldMapDialog = { open: true, close() { this.open = false; } };
  const recruitmentDialog = { open: false };
  const authCard = { scrollIntoView() {} };
  const context = vm.createContext({
    console,
    rotaryCompasses: new Map(),
    window: { innerWidth: compact ? 390 : 1280, setTimeout: (task) => timers.push(task) },
    document: { querySelector: (selector) => selector === ".auth-card" ? authCard : controls },
    normalizeProductId,
    productLaunchReady: true,
    queuedProductLaunch: null,
    state,
    WORKSPACE_ORDER: ["legal", "general", "finance"],
    STORAGE_KEYS: { activeProduct: "active", pendingProduct: "pending", workspace: "workspace" },
    storage: {
      async set(key, value) { storageWrites.push(["set", key, value]); },
      async remove(key) { storageWrites.push(["remove", key]); },
    },
    elements: { worldMapDialog, recruitmentDialog, resonanceDialog: "resonance", traceDialog: "trace" },
    updateProductSwitchers() {},
    closeOpenProductDialogs() {},
    setAuthMode(mode) { authModes.push(mode); },
    showToast(message) { toasts.push(message); },
    productDisplayName(product) { return PRODUCT_NAV_ITEMS.find((item) => item.id === product)?.label || product; },
    async changeWorkspace(product) { state.workspace = product; workspaces.push(product); },
    playWorkspaceEntry(product) { workspaces.push(product); },
    openConcept(product) { opened.push(product); },
    openOblivionArchive() { opened.push("oblivion"); },
    openRecruitment() { recruitmentDialog.open = true; opened.push("recruitment"); },
    openStudio() { opened.push("forge"); },
    openMusicDimension() { opened.push("music"); },
    openPhotonProjection() { opened.push("photon"); },
    productDomains: {
      openLeap() { opened.push("leap"); },
      openPulse() { opened.push("pulse"); },
    },
  });
  vm.runInContext(`${setupSource}\n${launchSource}`, context);
  const realLaunch = context.launchProduct;
  context.launchProduct = (product) => { launchCalls.push(product); return realLaunch(product); };
  // These are the real app's legacy generic card bindings. A compass click
  // must be consumed before one of these can open a second/wrong product.
  cards.forEach((card) => card.addEventListener("click", () => context.launchProduct(card.dataset.launch)));
  context.setupRotaryCompass(container);
  const compass = context.rotaryCompasses.get(id);
  function dispatch(type, target, options = {}) {
    const node = typeof target === "string" ? children.get(target) : target;
    const event = {
      type, target: node, pointerId: 1, button: 0, isPrimary: true, detail: 1,
      clientX: 100, clientY: 100, cancelable: true,
      ...options,
      prevented: false, stopped: false,
      preventDefault() { this.prevented = true; },
      stopImmediatePropagation() { this.stopped = true; },
    };
    const path = [];
    for (let current = node; current; current = current.parent) path.push(current);
    for (const phase of [true, false]) {
      const ordered = phase ? [...path].reverse() : path;
      for (const current of ordered) {
        for (const { handler, capture } of current.listeners.get(type) || []) {
          if (capture === phase) handler(event);
          if (event.stopped) return event;
        }
      }
    }
    return event;
  }
  function center(card) {
    return {
      clientX: Number.parseFloat(card.properties.get("--compass-x")),
      clientY: Number.parseFloat(card.properties.get("--compass-y")),
    };
  }
  function hitAt({ clientX, clientY }) {
    // The same transformed rectangles/z-index stacking as the flat CSS card
    // surfaces. Dispatch to the top surface, not to a preselected product.
    return cards.filter((card) => {
      if (card.getAttribute("aria-hidden") !== "false") return false;
      const point = center(card);
      const scale = Number.parseFloat(card.properties.get("--compass-scale"));
      return Math.abs(clientX - point.clientX) <= card.offsetWidth * scale / 2
        && Math.abs(clientY - point.clientY) <= card.offsetHeight * scale / 2;
    }).sort((a, b) => Number(b.style.zIndex) - Number(a.style.zIndex) || cards.indexOf(b) - cards.indexOf(a))[0];
  }
  return {
    container, cards, left, right, compass, dimensions, center, hitAt, launchCalls, opened, workspaces,
    state, worldMapDialog, recruitmentDialog, context, dispatch, storageWrites, authModes, toasts,
    flushTimers() { while (timers.length) timers.shift()(); },
    async settled() { await new Promise(setImmediate); },
  };
}

function selectProduct(runtimeState, productId) {
  const index = runtimeState.cards.findIndex((card) => card.dataset.launch === productId);
  runtimeState.compass.rotation = -index * Math.PI * 2 / runtimeState.cards.length;
  runtimeState.compass.render();
}

test("selected Future Radar centre is not covered and opens Radar", async () => {
  // Live measured height from the failing first-registration world map. At
  // the old fixed radius 94, EMBER covered Radar's centre by about 0.19px.
  const r = runtime({ cardHeight: 138.594 });
  const radar = r.cards.find((card) => card.dataset.launch === "recruitment");
  selectProduct(r, "recruitment");
  assert.equal(radar.dataset.compassSelected, "true");
  const point = r.center(radar);
  const target = r.hitAt(point);
  assert.equal(target, radar);
  r.dispatch("pointerdown", target, point);
  r.dispatch("pointerup", target, point);
  r.dispatch("click", target, point);
  await r.settled();
  assert.deepEqual(r.launchCalls, ["recruitment"]);
  assert.equal(r.recruitmentDialog.open, true);
  assert.deepEqual(r.workspaces, []);
});

for (const [product, label] of [["leap", "跃迁域"], ["pulse", "脉冲域"]]) {
  test(`unauthenticated ${product} selection keeps one global login and resumes after authentication`, async () => {
    const r = runtime({ token: null });
    await r.context.launchProduct(product);
    assert.equal(r.state.pendingLaunch, product);
    assert.deepEqual(r.opened, []);
    assert.deepEqual(r.authModes, ["login"]);
    assert.match(r.toasts[0], new RegExp(`登录冰焰后即可进入.*${label}`));
    assert.ok(r.storageWrites.some((entry) => entry[0] === "set" && entry[1] === "pending" && entry[2] === product));

    r.state.token = "frostfire-global-token";
    await r.context.launchProduct(product);
    assert.deepEqual(r.opened, [product]);
    assert.equal(r.state.pendingLaunch, null);
    assert.ok(r.storageWrites.some((entry) => entry[0] === "remove" && entry[1] === "pending"));
  });
}

test("authenticated switching across Leap, Pulse and Future Radar never re-authenticates", async () => {
  const r = runtime();
  await r.context.launchProduct("leap");
  await r.context.launchProduct("pulse");
  await r.context.launchProduct("recruitment");
  assert.deepEqual(r.opened, ["leap", "pulse", "recruitment"]);
  assert.deepEqual(r.authModes, []);
  assert.deepEqual(r.toasts, []);
});

test("visible card centres remain their own hit targets at every snapped map position", () => {
  for (const compact of [false, true]) {
    for (const id of ["world", "landing"]) {
      const r = runtime({ compact, id, cardHeight: id === "landing" ? 180 : 148 });
      for (let selected = 0; selected < r.cards.length; selected += 1) {
        r.compass.rotation = -selected * Math.PI * 2 / r.cards.length;
        r.compass.render();
        for (const card of r.cards) {
          if (card.getAttribute("aria-hidden") === "true") continue;
          assert.equal(r.hitAt(r.center(card)), card, `${compact}:${id}:${selected}:${card.dataset.launch}`);
        }
      }
    }
  }
});

test("font or card size changes recalculate centre clearance without changing the orbit during drag", () => {
  const r = runtime();
  const initial = r.compass.radiusY;
  r.dimensions.height = 176;
  r.compass.render();
  assert.ok(r.compass.radiusY > initial);
  const adjusted = r.compass.radiusY;
  r.dispatch("pointerdown", r.container);
  r.dispatch("pointermove", r.container, { clientX: 210 });
  assert.equal(r.compass.radiusY, adjusted);
  r.dispatch("pointerup", r.container, { clientX: 210 });
  for (const card of r.cards) {
    if (card.getAttribute("aria-hidden") === "false") assert.equal(r.hitAt(r.center(card)), card);
  }
});

test("small pointer jitter does not rotate, restack, or snap a card before activation", async () => {
  const r = runtime();
  selectProduct(r, "recruitment");
  const before = r.cards.map((card) => JSON.stringify([...card.properties]));
  r.dispatch("pointerdown", "recruitment");
  assert.equal(r.container.classList.contains("is-dragging"), false);
  r.dispatch("pointermove", "recruitment", { clientX: 103, clientY: 102 });
  r.dispatch("pointerup", "recruitment", { clientX: 103, clientY: 102 });
  assert.notEqual(r.compass.rotation, 0);
  assert.deepEqual(r.cards.map((card) => JSON.stringify([...card.properties])), before);
  assert.equal(r.container.classList.contains("is-snapping"), false);
  r.dispatch("click", "recruitment");
  await r.settled();
  assert.deepEqual(r.launchCalls, ["recruitment"]);
  assert.equal(r.recruitmentDialog.open, true);
  assert.equal(r.worldMapDialog.open, false);
  assert.equal(r.state.workspace, "general");
});

test("the pressed Future Radar product wins if an overlapping EMBER card receives click", async () => {
  const r = runtime();
  selectProduct(r, "recruitment");
  r.dispatch("pointerdown", "recruitment");
  r.dispatch("pointerup", "finance");
  const click = r.dispatch("click", "finance");
  await r.settled();
  assert.equal(click.prevented, true);
  assert.equal(click.stopped, true);
  assert.deepEqual(r.launchCalls, ["recruitment"]);
  assert.deepEqual(r.workspaces, []);
  assert.deepEqual(r.opened, ["recruitment"]);
});

test("dragging rotates but a delayed synthetic click cannot launch any product", async () => {
  const r = runtime();
  r.dispatch("pointerdown", "recruitment");
  r.dispatch("pointermove", r.container, { clientX: 205 });
  assert.notEqual(r.compass.rotation, 0);
  assert.equal(r.container.hasPointerCapture(1), true);
  r.dispatch("pointerup", r.container, { clientX: 205 });
  assert.equal(r.container.hasPointerCapture(1), false);
  r.flushTimers();
  r.dispatch("click", "finance");
  await r.settled();
  assert.deepEqual(r.launchCalls, []);
  assert.equal(r.worldMapDialog.open, true);
  // A new intentional press is usable immediately, without a time cooldown.
  const selected = r.cards[r.compass.selectedIndex].dataset.launch;
  r.dispatch("pointerdown", selected);
  r.dispatch("pointerup", selected);
  r.dispatch("click", selected);
  await r.settled();
  assert.deepEqual(r.launchCalls, [selected]);
});

test("cancelled gestures do not launch a product, even without prior movement", async () => {
  for (const cancellation of ["pointercancel", "pointerleave", "lostpointercapture"]) {
    const r = runtime();
    r.dispatch("pointerdown", "recruitment");
    r.dispatch(cancellation, r.container);
    r.flushTimers();
    r.dispatch("click", "recruitment");
    await r.settled();
    assert.deepEqual(r.launchCalls, [], cancellation);
    assert.equal(r.compass.dragging, false, cancellation);
  }
});

test("a gesture starting on the compass background cannot accidentally open a card", async () => {
  const r = runtime();
  r.dispatch("pointerdown", r.container);
  r.dispatch("pointerup", "finance");
  r.dispatch("click", "finance");
  await r.settled();
  assert.deepEqual(r.launchCalls, []);
});

test("secondary pointers cannot change or end the active gesture", () => {
  const r = runtime();
  selectProduct(r, "recruitment");
  const rotation = r.compass.rotation;
  r.dispatch("pointerdown", "recruitment");
  r.dispatch("pointerdown", "finance", { pointerId: 2, isPrimary: false });
  r.dispatch("pointermove", "finance", { pointerId: 2, clientX: 300 });
  r.dispatch("pointerup", "finance", { pointerId: 2 });
  assert.equal(r.compass.rotation, rotation);
  assert.equal(r.compass.dragging, true);
  assert.equal(r.compass.pressedCard.dataset.launch, "recruitment");
});

test("keyboard activation remains usable after a cancelled pointer gesture", async () => {
  const r = runtime();
  selectProduct(r, "recruitment");
  r.dispatch("pointerdown", "finance");
  r.dispatch("pointercancel", r.container);
  r.dispatch("click", "recruitment", { detail: 0 });
  await r.settled();
  assert.deepEqual(r.launchCalls, ["recruitment"]);
  assert.equal(r.recruitmentDialog.open, true);
});

test("all twelve desktop and mobile products activate their own destination exactly once", async () => {
  for (const compact of [false, true]) {
    for (const [index, product] of PRODUCT_NAV_ITEMS.entries()) {
      const r = runtime({ compact });
      r.compass.rotation = -index * Math.PI * 2 / r.cards.length;
      r.compass.render();
      assert.equal(r.cards[index].getAttribute("aria-hidden"), "false");
      r.dispatch("pointerdown", product.id);
      r.dispatch("pointerup", product.id);
      r.dispatch("click", product.id);
      await r.settled();
      assert.deepEqual(r.launchCalls, [product.id], `${compact}:${product.id}`);
      assert.equal(r.state.activeProduct, product.id);
      assert.equal(r.worldMapDialog.open, false);
      assert.deepEqual(product.dialogId ? r.opened : r.workspaces, [product.id]);
    }
  }
});

test("rotation controls and keyboard arrows only select, never navigate", async () => {
  const r = runtime();
  r.dispatch("click", r.right);
  assert.equal(r.container.dataset.selectedProduct, "finance");
  r.dispatch("keydown", r.container, { key: "ArrowRight" });
  assert.equal(r.container.dataset.selectedProduct, "general");
  r.dispatch("click", r.left);
  assert.equal(r.container.dataset.selectedProduct, "finance");
  await r.settled();
  assert.deepEqual(r.launchCalls, []);
});

test("reinitializing the same map cannot install duplicate activation handlers", async () => {
  const r = runtime({ id: "landing" });
  r.context.setupRotaryCompass(r.container);
  selectProduct(r, "recruitment");
  r.dispatch("click", "recruitment", { detail: 0 });
  await r.settled();
  assert.deepEqual(r.launchCalls, ["recruitment"]);
});

test("card hit targets are flat button surfaces, not independently interactive 3D children", () => {
  const css = readFileSync(new URL("./styles.css", import.meta.url), "utf8");
  const compass = css.match(/\.rotary-compass\s*\{([^}]+)\}/)[1];
  assert.match(compass, /transform-style:\s*flat/);
  assert.doesNotMatch(compass, /preserve-3d/);
  assert.match(css, /\.rotary-compass > \[data-launch\] > \*,[^}]+pointer-events:\s*none/);
});
