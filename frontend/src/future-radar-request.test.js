import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createServer } from "node:http";
import test from "node:test";
import vm from "node:vm";
import { createRadarPollingGate } from "./radar-polling.js";
import {
  DEFAULT_FUTURE_RADAR_STATUS,
  FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS,
  buildFutureRadarJobsQuery,
  futureRadarOpportunityErrorCopy,
  futureRadarTierQuery,
} from "./recruitment-radar.js";

const source = readFileSync(new URL("./app.js", import.meta.url), "utf8");
function extract(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start);
  return source.slice(start, end);
}

async function localServer(t, handler) {
  const requests = [];
  let received;
  const firstRequest = new Promise((resolve) => { received = resolve; });
  const server = createServer((request, response) => {
    requests.push({ url: request.url, method: request.method, authorization: request.headers.authorization });
    received();
    handler(request, response);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  t.after(async () => {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  });
  return { base: `http://127.0.0.1:${server.address().port}/api`, requests, firstRequest };
}

function runtime(base, { categories = [], onHeaders = () => {}, onLogout = null } = {}) {
  const state = { token: null, recruitmentTierFilter: "BALANCED", futureRadar: {
    page: 1, pageSize: 50,
    filters: { q: "", company: "", city: "", industry: "", employer_type: "", program_id: "",
      status: DEFAULT_FUTURE_RADAR_STATUS, verification_status: "", source_id: "", event_type: "", sort: "changed",
      opening_after: "", opening_before: "", closing_after: "", closing_before: "" },
  } };
  // Keep the real HTTP/fetch/AbortController chain. Only the clock is controllable,
  // so a stalled response can exercise the full 120-second deadline without a wait.
  const timers = [];
  let elapsed = 0;
  const context = {
    state, API_BASE: base, Headers, FormData, AbortController,
    radarPollingGate: createRadarPollingGate({ read: () => null, write() {}, locks: () => null }),
    FUTURE_RADAR_REQUEST_CONTROLLERS: new Set(),
    FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS, buildFutureRadarJobsQuery, futureRadarTierQuery,
    selectedRecruitmentStarfields: () => categories,
    fetch: async (...args) => {
      const response = await fetch(...args);
      onHeaders(response);
      return response;
    },
    setTimeout: (callback, delay) => {
      const timer = { callback, delay, deadline: elapsed + delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout: (timer) => { timer.cleared = true; },
    logout: (showMessage) => {
      if (onLogout) return onLogout(showMessage);
      throw new Error("Unexpected account flow in anonymous local test.");
    },
  };
  vm.createContext(context);
  vm.runInContext([
    extract("async function api(", "\nfunction showToast"),
    extract("function futureRadarJobsQuery(", "\nfunction syncFutureRadarSourceFilter"),
  ].join("\n"), context);
  return { state, timers, run: (expression) => vm.runInContext(expression, context),
    advance: async (ms) => {
      elapsed += ms;
      timers.filter((timer) => !timer.cleared && timer.deadline <= elapsed).forEach((timer) => {
        timer.cleared = true;
        timer.callback();
      });
      await Promise.resolve();
    },
  };
}

test("real builder and API send the active main-pool GET without blank dates or a cursor", async (t) => {
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ items: [{ id: "pending-example", status: "unknown", verification_status: "pending" }], total: 255 }));
  });
  const r = runtime(server.base);
  const data = await r.run("api(`/future-radar/opportunities?${futureRadarJobsQuery()}`, {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})");
  assert.equal(server.requests.length, 1);
  assert.deepEqual(server.requests[0], {
    url: "/api/future-radar/opportunities?page=1&page_size=50&status=active&sort=changed&compact=true&balanced_only=true&priority_only=false",
    method: "GET", authorization: undefined,
  });
  assert.equal(data.total, 255);
  assert.equal(data.items[0].verification_status, "pending");
  assert.equal(r.timers[0].delay, 120_000);
  assert.equal(r.timers[0].cleared, true);
});

test("real HTTP query preserves supported category, fractional T tier and dates", async (t) => {
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"items":[],"total":0}');
  });
  const r = runtime(server.base, { categories: ["policy_state_banks", "internet_tech", "policy_state_banks"] });
  r.state.recruitmentTierFilter = "T0.5";
  r.state.futureRadar.filters.company = "示例银行";
  r.state.futureRadar.filters.closing_after = "2026-08-30";
  r.state.futureRadar.filters.verification_status = "pending";
  await r.run("api(`/future-radar/opportunities?${futureRadarJobsQuery(2)}`, {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})");
  const params = new URL(server.requests[0].url, server.base).searchParams;
  assert.deepEqual(params.getAll("category"), ["policy_state_banks", "internet_tech"]);
  assert.equal(params.get("company"), "示例银行");
  assert.equal(params.get("page"), "2");
  assert.equal(params.get("tier_code"), "T0.5");
  assert.equal(params.get("balanced_only"), "false");
  assert.equal(params.get("priority_only"), "false");
  assert.equal(params.get("verification_status"), "pending");
  assert.equal(params.get("closing_after"), "2026-08-30");
  assert.equal(params.has("opening_after"), false);
  assert.equal(params.has("cursor"), false);
});

test("other API reads retain their existing 15-second timeout", async (t) => {
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"status":"ok"}');
  });
  const r = runtime(server.base);
  await r.run('api("/health")');
  assert.equal(r.timers[0].delay, 15_000);
  assert.equal(r.timers[0].cleared, true);
});

test("list and detail survive a 72.64-second cold read; status and authentication retain 15 seconds", async (t) => {
  for (const path of ["/future-radar/opportunities?page=1&compact=true", "/future-radar/opportunities/public-cold-read"]) {
    await t.test(path.includes("?") ? "list" : "detail", async (st) => {
      let respond;
      const server = await localServer(st, (_request, response) => {
        respond = () => {
          response.writeHead(200, { "Content-Type": "application/json" });
          response.end(JSON.stringify({ total: 3218, id: "public-cold-read" }));
        };
      });
      const r = runtime(server.base);
      let settled = false;
      const request = r.run(`api(${JSON.stringify(path)}, {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})`)
        .finally(() => { settled = true; });
      await server.firstRequest;
      await r.advance(72_640);
      assert.equal(settled, false);
      assert.equal(r.timers[0].cleared, false, "the cold read is not aborted at the old 45-second deadline");
      respond();
      assert.equal((await request).total, 3218);
      assert.equal(r.timers[0].delay, 120_000);
    });
  }
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" }); response.end("{}");
  });
  const r = runtime(server.base);
  await r.run('api("/future-radar/dashboard")');
  await r.run('api("/auth/me")');
  assert.deepEqual(r.timers.map((timer) => timer.delay), [15000, 15000]);
  assert.match(source, /api\("\/future-radar\/run",\s*\{[\s\S]*?timeoutMs: 120_000/,
    "scan-start deadline remains separate and unchanged");
});

test("a delayed real HTTP response aborts at the main-pool deadline with a distinct timeout error", async (t) => {
  const server = await localServer(t, () => {});
  const r = runtime(server.base);
  const request = r.run("api(`/future-radar/opportunities?${futureRadarJobsQuery()}`, {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})");
  const checked = assert.rejects(request, (error) => {
    assert.equal(error.code, "REQUEST_TIMEOUT");
    assert.equal(error.timeoutMs, 120_000);
    assert.match(futureRadarOpportunityErrorCopy(error), /读取超时/);
    assert.doesNotMatch(futureRadarOpportunityErrorCopy(error), /HTTP/);
    return true;
  });
  await server.firstRequest;
  assert.equal(r.timers[0].delay, 120_000);
  await r.advance(119_999);
  assert.equal(r.timers[0].cleared, false);
  await r.advance(1);
  await checked;
  assert.equal(r.timers[0].cleared, true);
});

test("the read timeout also covers a stalled JSON body after HTTP headers arrive", async (t) => {
  let headersReceived;
  const headers = new Promise((resolve) => { headersReceived = resolve; });
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.write('{"items":');
  });
  const r = runtime(server.base, { onHeaders: headersReceived });
  const request = r.run('api("/future-radar/opportunities/public-example", {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})');
  const checked = assert.rejects(request, (error) => {
    assert.equal(error.code, "REQUEST_TIMEOUT");
    assert.match(futureRadarOpportunityErrorCopy(error, true), /读取超时.*上次成功/);
    return true;
  });
  await headers;
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(r.timers[0].cleared, false, "JSON parsing is still inside the timed read");
  r.timers[0].callback();
  await checked;
  assert.equal(r.timers[0].cleared, true);
});

test("superseding a tier aborts the real HTTP read without reporting a timeout", async (t) => {
  const server = await localServer(t, () => {});
  const r = runtime(server.base);
  r.run("globalThis.selectionController = new AbortController()");
  const request = r.run('api("/future-radar/opportunities?tier_code=T1&compact=true", { signal: selectionController.signal, timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS })');
  const checked = assert.rejects(request, (error) => {
    assert.equal(error.code, "REQUEST_SUPERSEDED");
    assert.equal(error.timeoutMs, undefined);
    return true;
  });
  await server.firstRequest;
  r.run('selectionController.abort(Object.assign(new Error("selection changed"), {code: "REQUEST_SUPERSEDED"}))');
  await checked;
  assert.equal(r.timers[0].cleared, true);
  assert.equal(r.run("FUTURE_RADAR_REQUEST_CONTROLLERS.size"), 0);
});

test("an already superseded selection never sends its HTTP request", async (t) => {
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"items":[]}');
  });
  const r = runtime(server.base);
  r.run('globalThis.selectionController = new AbortController(); selectionController.abort(Object.assign(new Error("selection changed"), {code: "REQUEST_SUPERSEDED"}))');
  await assert.rejects(r.run('api("/future-radar/opportunities?tier_code=T2", {signal: selectionController.signal})'), (error) => error.code === "REQUEST_SUPERSEDED");
  await new Promise(setImmediate);
  assert.equal(server.requests.length, 0);
  assert.equal(r.run("FUTURE_RADAR_REQUEST_CONTROLLERS.size"), 0);
});

test("external cancellation also covers a stalled JSON body after response headers", async (t) => {
  let headersReceived;
  const headers = new Promise((resolve) => { headersReceived = resolve; });
  const server = await localServer(t, (_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.write('{"items":');
  });
  const r = runtime(server.base, { onHeaders: headersReceived });
  r.run("globalThis.selectionController = new AbortController()");
  const request = r.run('api("/future-radar/opportunities?tier_code=T3", {signal: selectionController.signal})');
  const checked = assert.rejects(request, (error) => error.code === "REQUEST_SUPERSEDED");
  await headers;
  r.run('selectionController.abort(Object.assign(new Error("selection changed"), {code: "REQUEST_SUPERSEDED"}))');
  await checked;
  assert.equal(r.timers[0].cleared, true);
});

for (const status of [422, 503]) {
  test(`HTTP ${status} stays distinguishable from client timeout without exposing upstream details`, async (t) => {
    const server = await localServer(t, (_request, response) => {
      response.writeHead(status, { "Content-Type": "application/json", "Retry-After": "2" });
      response.end('{"detail":"internal provider diagnostic example"}');
    });
    const r = runtime(server.base);
    await assert.rejects(r.run("api(`/future-radar/opportunities?${futureRadarJobsQuery()}`, {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})"), (error) => {
      assert.equal(error.status, status);
      assert.equal(error.retryAfter, "2");
      const copy = futureRadarOpportunityErrorCopy(error);
      assert.ok(copy.includes(`HTTP ${status}`));
      assert.doesNotMatch(copy, /读取超时|provider|diagnostic/);
      return true;
    });
  });
}

test("HTTP 401 requests reauthentication instead of an endless main-pool refresh", async (t) => {
  const server = await localServer(t, (_request, response) => {
    response.writeHead(401, { "Content-Type": "application/json" });
    response.end('{"detail":"Not authenticated"}');
  });
  const logoutCalls = [];
  const r = runtime(server.base, { onLogout: (showMessage) => logoutCalls.push(showMessage) });
  await assert.rejects(r.run("api(`/future-radar/opportunities?${futureRadarJobsQuery()}`, {timeoutMs: FUTURE_RADAR_OPPORTUNITY_READ_TIMEOUT_MS})"), (error) => {
    assert.equal(error.status, 401);
    const copy = futureRadarOpportunityErrorCopy(error, true);
    assert.match(copy, /HTTP 401.*请重新登录/);
    assert.doesNotMatch(copy, /刷新机会|读取超时/);
    return true;
  });
  assert.deepEqual(logoutCalls, [false]);
  assert.equal(server.requests[0].authorization, undefined);
});
