import test from "node:test";
import assert from "node:assert/strict";
import { revenueForecastCopy } from "./pulse-forecast.js";

test("shows the selected forecast and does not invent intervals for ML", () => {
  const copy = revenueForecastCopy({ status: "ok", selected_model: "numpy_lstm", horizon_months: 3,
    points: [{ period: "2027-01", revenue: 12300 }], ml_comparison: { status: "ok", selection_rule: "至少改善5%" } }, String);
  assert.match(copy, /LSTM/);
  assert.match(copy, /12300/);
  assert.match(copy, /3个月预测跨度/);
  assert.doesNotMatch(copy, /95%|NaN|线性趋势/);
});

test("short histories keep a clearly labelled trend reference", () => {
  const copy = revenueForecastCopy({ status: "ok", selected_model: "linear_trend", points: [],
    ml_comparison: { status: "insufficient_history" } }, String);
  assert.match(copy, /尚未满足多模型回测/);
});
