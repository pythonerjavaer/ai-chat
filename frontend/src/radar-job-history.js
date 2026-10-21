const dateTime = (value) => value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "持续有效";

export function createRadarJobHistory({ jobId, api, make }) {
  const panel = make("details", "job-tier-reason radar-job-history");
  panel.append(make("summary", "", "岗位变更与历史查询"));
  const body = make("div", "job-tier-reason-body");
  const form = make("form", "domain-actions");
  const input = (label) => {
    const wrapper = make("label", "", label);
    const field = make("input", "");
    field.type = "datetime-local";
    wrapper.append(field);
    form.append(wrapper);
    return field;
  };
  const validAt = input("查看哪一天的岗位情况（可留空）");
  const knownAt = input("按系统何时已知的信息查看（留空为现在）");
  const query = make("button", "job-watch-button", "查询历史");
  query.type = "submit";
  const output = make("div", "");
  const previous = make("button", "job-watch-button", "上一页");
  const next = make("button", "job-watch-button", "下一页");
  for (const button of [previous, next]) button.type = "button";
  form.append(query);
  body.append(form, output, previous, next);
  panel.append(body);
  let page = 1;
  let busy = false;
  let loaded = false;
  const load = async () => {
    if (busy) return;
    busy = true;
    query.disabled = previous.disabled = next.disabled = true;
    output.replaceChildren(make("small", "", "正在读取历史记录…"));
    try {
      const params = new URLSearchParams({ page: String(page), page_size: "20" });
      if (validAt.value) params.set("valid_at", new Date(validAt.value).toISOString());
      if (knownAt.value) params.set("known_at", new Date(knownAt.value).toISOString());
      const result = await api(`/future-radar/jobs/${encodeURIComponent(jobId)}/history?${params}`);
      output.replaceChildren(make("small", "", `历史记录开始于 ${dateTime(result.knowledge_since)}；此前状态不作推断。`));
      if (!result.items?.length) output.append(make("p", "", "这个时间范围内没有已核验的历史记录。"));
      for (const version of result.items || []) {
        const row = make("article", "radar-entity-card");
        const job = version.job || {};
        row.append(make("strong", "", `${job.company || ""} · ${job.title || ""}`),
          make("p", "", `状态：${{ open: "开放", closed: "已关闭", unknown: "待确认" }[job.status] || "待确认"} · 地点：${job.city || job.region || "待确认"} · 截止：${job.closing_date || "未公布"}`),
          make("small", "", `岗位事实适用期间：${dateTime(version.valid_from)} — ${dateTime(version.valid_to)}`),
          make("small", "", `系统持有此记录期间：${dateTime(version.system_from)} — ${dateTime(version.system_to)}`));
        if (version.origin === "confirmed_correction") row.append(make("p", "", `已核实历史更正：${version.reason}`));
        output.append(row);
      }
      previous.disabled = page <= 1;
      next.disabled = !result.has_more;
      loaded = true;
    } catch (error) {
      output.replaceChildren(make("p", "", `暂时无法读取岗位历史：${error.message}`));
    } finally {
      busy = false;
      query.disabled = false;
    }
  };
  form.addEventListener("submit", (event) => { event.preventDefault(); page = 1; void load(); });
  previous.addEventListener("click", () => { page = Math.max(1, page - 1); void load(); });
  next.addEventListener("click", () => { page += 1; void load(); });
  panel.addEventListener("toggle", () => { if (panel.open && !loaded) void load(); });
  return panel;
}
