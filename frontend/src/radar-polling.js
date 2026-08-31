// Transport backoff only: never changes a source interval or a server run lock.
export const RADAR_STATUS_INTERVAL_MS = 15_000;
const KEY = "frostfire_radar_transport_v1";
const MAX_FAILURES = 5;

export function retryAfterMilliseconds(value, now = Date.now()) {
  if (value == null || value === "") return 0;
  const seconds = Number(value);
  if (Number.isFinite(seconds)) return Math.max(0, seconds * 1000);
  const date = Date.parse(value);
  return Number.isFinite(date) ? Math.max(0, date - now) : 0;
}

export function createRadarPollingGate({
  now = () => Date.now(),
  read = () => globalThis.localStorage?.getItem(KEY),
  write = (value) => globalThis.localStorage?.setItem(KEY, value),
  locks = () => globalThis.navigator?.locks,
} = {}) {
  let memory = {}, dashboardPromise = null, dashboardScope = null, cached = null, cachedAt = 0, generation = 0;
  const load = () => {
    try { const raw = read(); if (raw) memory = JSON.parse(raw); } catch (_) { /* memory fallback */ }
    return memory && typeof memory === "object" ? memory : {};
  };
  const save = (value) => {
    memory = value;
    // Only counters/timestamps: no account, token, response or source content.
    try { write(JSON.stringify(value)); } catch (_) { /* memory fallback */ }
  };
  const deferred = (delay, suspended = false) => Object.assign(new Error(suspended
    ? "自动跟踪已暂停；请稍后点击刷新机会重试。"
    : "服务暂时繁忙，正在等待后重试。"), {
    code: suspended ? "RADAR_POLL_SUSPENDED" : "RADAR_POLL_DEFERRED",
    retryAfter: Math.ceil(delay / 1000),
  });
  const gate = {
    delay(minimum = 0) { return Math.max(minimum, Number(load().retryAt || 0) - now()); },
    suspended() { return load().suspended === true; },
    assertAllowed() {
      const value = load();
      if (value.suspended || Number(value.retryAt || 0) > now()) {
        throw deferred(gate.delay(), value.suspended === true);
      }
    },
    failure(error) {
      if (![429, 500, 502, 503, 504].includes(Number(error.status))
        && error.code !== "REQUEST_TIMEOUT" && error.name !== "TypeError" && error.name !== "AbortError") return;
      const value = load();
      const retry = retryAfterMilliseconds(error.retryAfter, now());
      // Concurrent failures from one snapshot count as one failed attempt.
      if (Number(value.retryAt || 0) > now()) {
        if (retry) save({ ...value, retryAt: Math.max(value.retryAt, now() + retry) });
        return;
      }
      const failures = Math.min(MAX_FAILURES, Number(value.failures || 0) + 1);
      save({ ...value, failures, suspended: failures >= MAX_FAILURES,
        retryAt: now() + Math.max(retry, Math.min(240_000, RADAR_STATUS_INTERVAL_MS * 2 ** failures)) });
    },
    success() {
      const value = load();
      // A late successful sibling request must not erase newer rate limiting.
      if (!value.suspended && Number(value.retryAt || 0) <= now() && value.failures) {
        save({ ...value, failures: 0, retryAt: 0 });
      }
    },
    resume() {
      const value = load();
      // Explicit user retry may reset the finite budget, not Retry-After.
      save({ ...value, failures: 0, suspended: false });
    },
    invalidateDashboard() {
      generation += 1;
      cached = null; cachedAt = 0; dashboardPromise = null;
      // Do not reset shared dashboardAt, Retry-After or the finite retry budget.
    },
    clearSession() { gate.invalidateDashboard(); dashboardScope = null; },
    dashboard(fetchDashboard, scope) {
      if (dashboardPromise && dashboardScope === scope) return dashboardPromise;
      if (dashboardScope !== scope) gate.invalidateDashboard();
      dashboardScope = scope;
      const requestGeneration = generation;
      const assertCurrent = () => {
        if (requestGeneration !== generation || dashboardScope !== scope) throw deferred(RADAR_STATUS_INTERVAL_MS);
      };
      const run = async () => {
        assertCurrent();
        gate.assertAllowed();
        if (cached && dashboardScope === scope && now() - cachedAt < RADAR_STATUS_INTERVAL_MS) return cached;
        const acquire = async (lock = true) => {
          assertCurrent();
          gate.assertAllowed();
          const value = load();
          if (!lock || Number(value.dashboardAt || 0) > now()) throw deferred(RADAR_STATUS_INTERVAL_MS);
          // Web Locks makes this reservation atomic across tabs. Timestamp also
          // bounds retries across reloads / browsers without Web Locks support.
          save({ ...value, dashboardAt: now() + RADAR_STATUS_INTERVAL_MS });
          const result = await fetchDashboard();
          assertCurrent();
          cached = result; cachedAt = now();
          return result;
        };
        const manager = locks();
        return manager?.request
          ? manager.request("frostfire-radar-dashboard", { ifAvailable: true }, acquire)
          : acquire();
      };
      const promise = Promise.resolve().then(run).finally(() => {
        if (dashboardPromise === promise) dashboardPromise = null;
      });
      dashboardPromise = promise;
      return promise;
    },
  };
  return gate;
}

export const radarPollingGate = createRadarPollingGate();
