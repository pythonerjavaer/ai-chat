export function sourceHealthStatus(source = {}) {
  if (source.enabled === false) return "disabled";
  return String(source.status || source.health || "pending").toLowerCase();
}

export function sourceHealthGroup(source) {
  const status = sourceHealthStatus(source);
  if (status === "disabled") return "disabled";
  if (["error", "failed"].includes(status)) return "error";
  if (["healthy", "idle", "success"].includes(status)) return "healthy";
  if (["discovery_limited", "access_restricted"].includes(status)) return "limited";
  return "pending";
}

export function sourceHealthCounts(sources = []) {
  const counts = { all: 0, error: 0, healthy: 0, limited: 0, pending: 0 };
  for (const source of sources) {
    const group = sourceHealthGroup(source);
    if (group === "disabled") continue;
    counts.all += 1;
    counts[group] += 1;
  }
  return counts;
}

export function filterSourcesByHealth(sources = [], filter = "all") {
  return sources.filter((source) => {
    const group = sourceHealthGroup(source);
    return group !== "disabled" && (filter === "all" || group === filter);
  });
}

export function sourceHealthExplanation(source) {
  switch (sourceHealthGroup(source)) {
    case "healthy": return "最近一次读取成功；这不代表当前一定有适合你的岗位。";
    case "error": return "最近一次读取失败；下方可查看原因并重新核验，已有岗位仍保留。";
    case "limited": return sourceHealthStatus(source) === "discovery_limited"
      ? "尚未接通可用的自动发现渠道。当前不会自动获取该公众号的新文章，也不会调用付费 AI；已有招聘线索仍保留。"
      : "当前网站限制自动访问，暂时无法核验；可打开原始页面查看。";
    default: return "尚未完成一次有效核验，不能判断为健康或异常。";
  }
}
