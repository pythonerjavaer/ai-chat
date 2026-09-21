import test from "node:test";
import assert from "node:assert/strict";
import { sourceHealthCounts, filterSourcesByHealth, sourceHealthExplanation } from "./radar-source-health.js";

const sources = [
  { id: "ok", status: "healthy", last_error: "historical failure" },
  { id: "broken", status: "error" },
  { id: "wechat", status: "discovery_limited" },
  { id: "unread", status: "pending" },
  { id: "retired", status: "error", enabled: false },
];

test("abnormal filter excludes healthy, limited, unchecked and disabled sources", () => {
  assert.deepEqual(filterSourcesByHealth(sources, "error").map(s => s.id), ["broken"]);
  assert.deepEqual(sourceHealthCounts(sources), { all: 4, healthy: 1, error: 1, limited: 1, pending: 1 });
  assert.deepEqual(filterSourcesByHealth(sources).map(s => s.id), ["ok", "broken", "wechat", "unread"]);
});

test("successful retry removes only the recovered source from the abnormal list", () => {
  const repaired = sources.map(s => s.id === "broken" ? { ...s, status: "healthy" } : s);
  assert.deepEqual(filterSourcesByHealth(repaired, "error"), []);
  assert.equal(sourceHealthCounts(repaired).healthy, 2);
});

test("limited source explanation does not imply successful automatic collection", () => {
  assert.match(sourceHealthExplanation(sources[2]), /不会自动获取.*不会调用付费 AI/);
  assert.match(sourceHealthExplanation(sources[0]), /不代表当前一定有适合你的岗位/);
});
