const fields = { company: 160, title: 240, batch: 160, location: 160, notes: 1000, confirmed_date: 10 };

export async function prepareApplicationRecords(value) {
  if (!Array.isArray(value) || !value.length || value.length > 100) throw new Error('请提供 1–100 条已确认报名记录的 JSON 数组。');
  const records = [];
  const keys = new Set();
  for (const [index, row] of value.entries()) {
    if (!row || typeof row !== 'object' || Array.isArray(row)
      || Object.keys(row).some(key => !Object.hasOwn(fields, key) && key !== 'record_key')) throw new Error(`第 ${index + 1} 条包含不支持的字段。`);
    const body = {};
    for (const [name, limit] of Object.entries(fields)) {
      if (row[name] != null && typeof row[name] !== 'string') throw new Error(`第 ${index + 1} 条的 ${name} 必须是文字。`);
      const text = row[name]?.trim() || null;
      if (text && text.length > limit) throw new Error(`第 ${index + 1} 条的 ${name} 太长。`);
      if (text && /chatgpt\.com\/|chat\.openai\.com\/|chatgpt:\/\/|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i.test(text)) throw new Error('请勿导入聊天链接或聊天标识，只保留报名事实。');
      body[name] = text;
    }
    if (!body.company) throw new Error(`第 ${index + 1} 条缺少公司。`);
    if (body.confirmed_date && (!/^\d{4}-\d{2}-\d{2}$/.test(body.confirmed_date)
      || Number.isNaN(Date.parse(body.confirmed_date))
      || new Date(body.confirmed_date).toISOString().slice(0, 10) !== body.confirmed_date)) throw new Error(`第 ${index + 1} 条的确认日期无效。`);
    const identity = JSON.stringify(['company', 'title', 'batch', 'location'].map(key => (body[key] || '').normalize('NFKC').toLowerCase().replace(/\s+/g, ' ').trim()));
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(identity));
    const key = row.record_key || `manual-${[...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('')}`;
    if (typeof key !== 'string' || !/^[A-Za-z0-9_.:-]{1,120}$/.test(key)) throw new Error(`第 ${index + 1} 条的记录标识无效。`);
    if (keys.has(key)) continue;
    keys.add(key); records.push({ key, body });
  }
  return records;
}

export function initApplicationRecords({ api, session, toast }) {
  const node = (tag, text = '', className = '') => { const item = document.createElement(tag); item.textContent = text; item.className = className; return item; };
  const panel = node('section', '', 'radar-application-history');
  panel.append(node('h4', '历史报名台账'), node('p', '记录你本人已确认报名的单位。可以只按单位记录，具体岗位选填。'));
  const list = node('div', '', 'radar-entity-list');
  const progress = node('p', '点击“已报名”读取历史报名台账。'); progress.setAttribute('role', 'status');
  const pagination = node('nav', '', 'radar-pagination'); pagination.setAttribute('aria-label', '历史报名台账分页');
  panel.append(progress, list, pagination);
  const details = node('details'); details.append(node('summary', '补录已报名'));
  const form = node('form', '', 'radar-filter-bar');
  const inputs = {};
  for (const [name, title] of Object.entries({ company: '公司（必填）', title: '具体岗位（可空）', batch: '批次（可空）', location: '地点（可空）', confirmed_date: '确认报名日期（可空）', notes: '备注（可空）' })) {
    const label = node('label'); label.append(node('span', title));
    const input = node('input'); input.type = name === 'confirmed_date' ? 'date' : 'text'; input.maxLength = fields[name]; input.required = name === 'company'; input.setAttribute('aria-label', title);
    inputs[name] = input; label.append(input); form.append(label);
  }
  const save = node('button', '保存已确认报名'); save.type = 'submit'; form.append(save);
  details.append(form);
  const batchDetails = node('details'); batchDetails.append(node('summary', '导入已确认报名记录'));
  batchDetails.append(node('p', '粘贴 JSON 数组，先预览再保存。字段：company、title、batch、location、notes、confirmed_date；除 company 外均可空。不要放入聊天链接或私人对话全文。'));
  const textarea = node('textarea'); textarea.id = 'radar-application-records-json'; textarea.rows = 5; textarea.maxLength = 200000; textarea.setAttribute('aria-label', '已确认报名记录 JSON');
  const previewButton = node('button', '预览导入'); previewButton.type = 'button'; previewButton.id = 'radar-application-records-preview';
  const importButton = node('button', '保存预览中的报名记录'); importButton.type = 'button'; importButton.disabled = true; importButton.id = 'radar-application-records-import';
  const preview = node('div'); preview.setAttribute('aria-live', 'polite');
  const saveStatus = node('p'); saveStatus.setAttribute('role', 'status');
  batchDetails.append(textarea, previewButton, preview, importButton); details.append(batchDetails, saveStatus); panel.append(details);
  let page = 1, request = 0, saving = false, prepared = [], preparation = 0;

  async function load(nextPage = 1) {
    const token = session(); if (!token) return;
    const current = ++request; progress.textContent = '正在读取历史报名台账…';
    try {
      const payload = await api(`/future-radar/application-records?page=${nextPage}&page_size=100`, { timeoutMs: 180000 });
      if (token !== session() || current !== request) return;
      const total = Number(payload.total) || 0;
      const pages = Math.max(1, Math.ceil(total / 100));
      if (nextPage > pages) return load(pages);
      page = nextPage; list.replaceChildren();
      for (const record of payload.items || []) {
        const card = node('article', '', 'radar-entity-card');
        card.append(node('span', '已报名 · 本人确认', 'radar-application-badge'), node('h4', record.company), node('p', record.title || '按单位记录'));
        const meta = [record.batch, record.location, record.confirmed_date ? `确认日期 ${record.confirmed_date}` : ''].filter(Boolean).join(' · ');
        if (meta) card.append(node('p', meta));
        if (record.notes) card.append(node('p', record.notes));
        const remove = node('button', '移除报名记录'); remove.type = 'button';
        remove.addEventListener('click', async () => {
          const owner = session(); remove.disabled = true;
          try {
            await api(`/future-radar/application-records/${encodeURIComponent(record.record_key)}`, { method: 'DELETE', timeoutMs: 180000 });
            if (owner === session()) { toast('报名记录已移除'); await load(page); }
          } catch (_) { if (owner === session()) toast('移除失败，请重试'); }
          finally { if (owner === session()) remove.disabled = false; }
        }); card.append(remove); list.append(card);
      }
      progress.textContent = total ? `共 ${total} 条历史报名记录` : '暂无历史报名记录，可展开“补录已报名”保存已有报名。';
      pagination.replaceChildren();
      if (pages > 1) {
        const previous = node('button', '← 上一页'); previous.type = 'button'; previous.disabled = page <= 1; previous.addEventListener('click', () => load(page - 1));
        const next = node('button', '下一页 →'); next.type = 'button'; next.disabled = page >= pages; next.addEventListener('click', () => load(page + 1));
        pagination.append(previous, node('span', `第 ${page} / ${pages} 页`), next);
      }
    } catch (_) {
      if (token !== session() || current !== request) return;
      progress.replaceChildren(node('span', '历史报名台账读取失败。'));
      const retry = node('button', '重试读取历史报名'); retry.type = 'button'; retry.addEventListener('click', () => load(page)); progress.append(retry);
    }
  }
  async function submit(records) {
    if (saving || !records.length || !session()) return;
    const token = session(); saving = true; save.disabled = true; importButton.disabled = true; textarea.disabled = true; previewButton.disabled = true;
    let completed = 0;
    try {
      for (const record of records) {
        if (token !== session()) return;
        saveStatus.textContent = `正在保存 ${completed + 1} / ${records.length} 条…`;
        await api(`/future-radar/application-records/${encodeURIComponent(record.key)}`, { method: 'PUT', body: JSON.stringify(record.body), timeoutMs: 180000 });
        if (token !== session()) return;
        ++completed;
      }
      saveStatus.textContent = `已保存 ${completed} 条本人确认的报名记录。`; prepared = []; preview.replaceChildren();
      toast(`已保存 ${completed} 条报名记录`); await load(1);
    } catch (_) {
      if (token === session()) { saveStatus.textContent = `已保存 ${completed} / ${records.length} 条；其余保存失败，请重试。重复提交相同记录不会重复新增。`; await load(page); }
    } finally {
      if (token === session()) { saving = false; save.disabled = false; textarea.disabled = false; previewButton.disabled = false; importButton.disabled = !prepared.length; }
    }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault(); const token = session();
    try { const records = await prepareApplicationRecords([Object.fromEntries(Object.entries(inputs).map(([name, input]) => [name, input.value || null]))]); if (token === session()) await submit(records); }
    catch (error) { if (token === session()) saveStatus.textContent = error.message; }
  });
  textarea.addEventListener('input', () => { ++preparation; prepared = []; importButton.disabled = true; preview.replaceChildren(); });
  previewButton.addEventListener('click', async () => {
    const token = session(), current = ++preparation;
    prepared = []; importButton.disabled = true;
    try {
      const records = await prepareApplicationRecords(JSON.parse(textarea.value));
      if (token !== session() || current !== preparation) return;
      prepared = records; preview.replaceChildren(node('p', `将保存 ${records.length} 条已确认报名记录：`));
      const names = node('ul'); records.forEach(({body}) => names.append(node('li', `${body.company} · ${body.title || '按单位记录'}${body.batch ? ` · ${body.batch}` : ''}`))); preview.append(names); importButton.disabled = false;
    } catch (error) { if (token === session() && current === preparation) preview.replaceChildren(node('p', error instanceof SyntaxError ? 'JSON 格式不正确，请检查后重新预览。' : error.message)); }
  });
  importButton.addEventListener('click', () => submit(prepared));
  function reset() {
    ++request; ++preparation; page = 1; saving = false; prepared = [];
    list.replaceChildren(); pagination.replaceChildren(); preview.replaceChildren(); saveStatus.textContent = ''; progress.textContent = '点击“已报名”读取历史报名台账。';
    Object.values(inputs).forEach(input => { input.value = ''; }); textarea.value = ''; textarea.disabled = false; save.disabled = false; previewButton.disabled = false; importButton.disabled = true; details.open = false; batchDetails.open = false;
  }
  return { panel, load, reset };
}
