export const REVENUE_MODEL_NAMES = {
  linear_trend: "线性趋势基线", last_value_baseline: "上月值基线",
  seasonal_naive: "去年同月基线", random_forest: "随机森林", adaboost: "AdaBoost",
  bayesian_ridge: "贝叶斯岭回归", numpy_lstm: "LSTM",
};

export function revenueForecastCopy(forecast, money) {
  if (forecast.status !== "ok") return `月收入样本不足（${forecast.observations || 0}/${forecast.minimum_months || 3}个月），暂不外推。`;
  const model = REVENUE_MODEL_NAMES[forecast.selected_model] || "线性趋势基线";
  const points = (forecast.points || []).map((point) => {
    const interval = Number.isFinite(point.lower_95) && Number.isFinite(point.upper_95)
      ? `（95%预测区间 ${money(Math.round(point.lower_95))}–${money(Math.round(point.upper_95))}）` : "";
    return `${point.period} ${money(Math.round(point.revenue))}${interval}`;
  }).join(" · ");
  const comparison = forecast.ml_comparison;
  const evidence = comparison?.status === "incomplete_history"
    ? `缺少${forecast.missing_months?.length || 0}个月的明确收入记录；暂以零值显示趋势参考，补齐账目或明确零收入月份后再做模型回测。`
    : comparison?.status === "ok"
    ? `按${forecast.horizon_months}个月预测跨度滚动回测；${comparison.selection_rule}。`
    : "当前仅显示短历史趋势参考，尚未满足多模型回测的数据门槛。";
  return `${model}：${points}。${evidence}`;
}
