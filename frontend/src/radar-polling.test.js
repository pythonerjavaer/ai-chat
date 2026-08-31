import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createRadarPollingGate, RADAR_STATUS_INTERVAL_MS, retryAfterMilliseconds } from "./radar-polling.js";

function fixture() {
  let time = 1_000_000, persisted = null;
  const options = { now: () => time, read: () => persisted, write: (value) => { persisted = value; }, locks: () => null };
  return { gate: createRadarPollingGate(options), another: () => createRadarPollingGate(options),
    advance: (ms) => { time += ms; }, persisted: () => JSON.parse(persisted) };
}

test("status polling normally waits 15 seconds and shares in-flight/dashboard cache", async () => {
  const f = fixture(); let calls = 0, finish;
  const fetcher = () => { calls++; return new Promise((resolve) => { finish = resolve; }); };
  const a = f.gate.dashboard(fetcher, "session-a"), b = f.gate.dashboard(fetcher, "session-a");
  assert.equal(a, b); await Promise.resolve(); assert.equal(calls, 1);
  finish({ running: true }); await a;
  await f.gate.dashboard(fetcher, "session-a"); assert.equal(calls, 1);
  assert.equal(RADAR_STATUS_INTERVAL_MS, 15000);
  await assert.rejects(f.another().dashboard(fetcher, "session-a"), { code: "RADAR_POLL_DEFERRED" });
  assert.equal(calls, 1, "another tab / reload cannot reissue dashboard inside the shared interval");
});

test("network errors back off exponentially, respect Retry-After and cannot reset by reload", () => {
  const f = fixture();
  f.gate.failure({ status: 502 }); assert.equal(f.gate.delay(), 30000);
  f.gate.failure({ status: 502 }); assert.equal(f.persisted().failures, 1, "same failed wave counted once");
  f.gate.failure({ status: 429, retryAfter: "120" }); assert.equal(f.gate.delay(), 120000);
  assert.throws(() => f.another().assertAllowed(), { code: "RADAR_POLL_DEFERRED" });
  f.gate.success(); assert.equal(f.gate.delay(), 120000, "late sibling success cannot cancel backoff");
  f.advance(120000); f.gate.failure({ status: 503 }); assert.equal(f.gate.delay(), 60000);
  assert.equal(retryAfterMilliseconds("Thu, 01 Jan 1970 00:02:00 GMT", 0), 120000);
});

test("five consecutive failed attempts suspend automated retries across tabs and reloads", () => {
  const f = fixture();
  for (let n = 0; n < 5; n++) { f.gate.failure({ status: 429 }); f.advance(f.gate.delay()); }
  assert.equal(f.gate.suspended(), true);
  assert.throws(() => f.another().assertAllowed(), { code: "RADAR_POLL_SUSPENDED" });
  f.gate.resume(); f.gate.assertAllowed(); assert.equal(f.gate.suspended(), false);
});

test("explicit retry still respects Retry-After and no account data is persisted", () => {
  const f = fixture(); f.gate.failure({ status: 429, retryAfter: "180" }); f.gate.resume();
  assert.throws(() => f.gate.assertAllowed(), { code: "RADAR_POLL_DEFERRED" });
  assert.deepEqual(Object.keys(f.persisted()).sort(), ["failures", "retryAt", "suspended"]);
});

test("dashboard cache never crosses sessions; unavailable storage has bounded memory fallback", async () => {
  let time = 100000;
  const gate = createRadarPollingGate({ now: () => time, read() { throw Error("disabled"); }, write() { throw Error("disabled"); }, locks: () => null });
  await gate.dashboard(async () => ({ account: "a" }), "a");
  time += 15000;
  assert.deepEqual(await gate.dashboard(async () => ({ account: "b" }), "b"), { account: "b" });
  gate.failure({ status: 502 }); assert.throws(() => gate.assertAllowed(), { code: "RADAR_POLL_DEFERRED" });
});

const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
function slice(start, end) { return source.slice(source.indexOf(start), source.indexOf(end, source.indexOf(start))); }
function runtime() {
  const f = fixture(), timers = [], calls = [];
  const state = { token: "local-test", futureRadar: { dashboard: {}, runs: [], totalJobs: 1,
    runStatusPollTimer: {}, runStatusPollPending: {}, runStatusTracking: { quick: true, deep: true },
    activeRunTypes: new Set(["quick"]), terminalSnapshotPromise: null } };
  const elements = { recruitmentDialog: { open: true } }, document = { hidden: false };
  const context = { state, elements, document, radarPollingGate: f.gate, FUTURE_RADAR_SCAN_TYPES: ["quick", "deep"],
    FUTURE_RADAR_RUN_STATUS_POLL_MS: 15000, FUTURE_RADAR_MANUAL_DEBOUNCE_SECONDS: 20,
    window: { clearTimeout(timer) { if (timer) timer.cleared = true; }, setTimeout(callback, delay) { const timer = { callback, delay }; timers.push(timer); return timer; } },
    api: async () => { calls.push("dashboard"); return { active: ["quick"] }; },
    futureRadarActiveRunTypes: (dashboard) => dashboard.active || [],
    renderFutureRadarDashboard() {}, setFutureRadarLoading() {}, setFutureRadarActionStatus() {},
    markFutureRadarRunActive() {}, showToast() {}, startFutureRadarRunDelay() {}, futureRadarRunTone() {},
    futureRadarRunSuccessCopy() {}, loadFutureRadarSnapshot: async () => { calls.push("snapshot"); return true; },
  };
  vm.createContext(context);
  vm.runInContext(slice("function stopFutureRadarRunStatusPolling(", "\nfunction renderFutureRadarDashboard"), context);
  return { ...f, state, elements, document, context, timers, calls, run: (code) => vm.runInContext(code, context) };
}

test("hidden/closed radar sends no status requests and reopening schedules bounded resumption", async () => {
  const r = runtime(); r.document.hidden = true;
  await r.run('pollFutureRadarRunUntilTerminal("quick")'); assert.equal(r.calls.length, 0);
  r.document.hidden = false; r.elements.recruitmentDialog.open = false;
  await r.run('pollFutureRadarRunUntilTerminal("quick")'); assert.equal(r.calls.length, 0);
  r.elements.recruitmentDialog.open = true; r.run("resumeFutureRadarRunStatusPolling()");
  assert.ok(r.timers.length > 0); assert.ok(r.timers.every((timer) => timer.delay >= 15000));
});

test("a failed tracking request does not reissue a scan; backoff controls subsequent scheduling", async () => {
  const r = runtime(); r.context.api = async () => {
    r.calls.push("dashboard"); const error = Object.assign(Error("busy"), { status: 429, retryAfter: "90" });
    r.gate.failure(error); throw error;
  };
  await r.run('pollFutureRadarRunUntilTerminal("quick")');
  assert.deepEqual(r.calls, ["dashboard"]); assert.equal(r.timers.at(-1).delay, 90000);
  assert.equal(r.state.futureRadar.runStatusPollPending.quick, false);
});

test("Quick and Deep terminal status share one dashboard and one final snapshot", async () => {
  const r = runtime(); r.context.api = async () => { r.calls.push("dashboard"); return { active: [] }; };
  await r.run('Promise.all([pollFutureRadarRunUntilTerminal("quick"), pollFutureRadarRunUntilTerminal("deep")])');
  assert.equal(r.calls.filter((call) => call === "dashboard").length, 1);
  assert.equal(r.calls.filter((call) => call === "snapshot").length, 1);
  assert.equal(r.state.futureRadar.runStatusTracking.quick, false);
  assert.equal(r.state.futureRadar.runStatusTracking.deep, false);
});

test("post-409/timeout tracking cannot complete from the pre-scan idle dashboard cache", async () => {
  const r = runtime();
  await r.gate.dashboard(async () => ({ active: [] }), r.state.token);
  const before = r.persisted().dashboardAt;
  r.run('startFutureRadarRunStatusPolling("quick")');
  await new Promise(setImmediate);
  assert.equal(r.calls.includes("snapshot"), false, "cached idle does not trigger completion");
  assert.equal(r.persisted().dashboardAt, before, "invalidation does not bypass the cross-tab throttle");
  assert.equal(r.timers.at(-1).delay, 15000);
  r.advance(15000);
  await r.run('pollFutureRadarRunUntilTerminal("quick")');
  assert.deepEqual(r.calls, ["dashboard"], "fresh running status is required after scan start");
  assert.equal(r.state.futureRadar.runStatusTracking.quick, true);
});

test("old in-flight idle responses are discarded after scan start and after session reset", async () => {
  const f = fixture(); let finish;
  const previous = f.gate.dashboard(() => new Promise((resolve) => { finish = resolve; }), "session-a");
  const rejected = assert.rejects(previous, { code: "RADAR_POLL_DEFERRED" });
  await Promise.resolve();
  f.gate.invalidateDashboard();
  finish({ active: [] }); await rejected;
  f.advance(15000);
  assert.deepEqual(await f.gate.dashboard(async () => ({ active: ["quick"] }), "session-a"), { active: ["quick"] });

  f.advance(15000);
  const pending = f.gate.dashboard(() => new Promise((resolve) => { finish = resolve; }), "session-a");
  const ended = assert.rejects(pending, { code: "RADAR_POLL_DEFERRED" });
  await Promise.resolve(); f.gate.clearSession();
  finish({ active: [], account: "old" }); await ended;
  f.advance(15000);
  assert.deepEqual(await f.gate.dashboard(async () => ({ account: "new" }), "session-b"), { account: "new" });
});
