export function initRadarPersonal({ api, session, host, makeCard, toast }) {
  let saved = new Map();
  let owner = null;
  let timer = null;
  let pending = false;
  let notice = null;
  const panel = document.createElement('section');
  panel.dataset.radarPanel = 'saved';
  panel.className = 'radar-tab-panel hidden';
  host.querySelector('[data-radar-panel="jobs"]').after(panel);

  function node(tag, text, className = '') {
    const result = document.createElement(tag);
    result.textContent = text;
    result.className = className;
    return result;
  }
  function updateButtons() {
    document.querySelectorAll('[data-save-job]').forEach(button => {
      const active = saved.has(button.dataset.saveJob);
      button.textContent = active ? '★ 已收藏 · 取消收藏' : '☆ 收藏到报名清单';
      button.setAttribute('aria-pressed', String(active));
    });
  }
  function renderSaved() {
    panel.replaceChildren(node('h3', '我的报名清单'), node('p', '数字越小越先报名。收藏和顺序随账号保存；取消收藏不会删除机会池中的岗位。'));
    if (!saved.size) panel.append(node('p', '还没有收藏岗位。展开企业岗位后，点击“收藏到报名清单”。'));
    [...saved.values()].sort((a, b) => a.priority - b.priority).forEach(item => {
      const card = makeCard(item.job);
      if (item.unavailable) card.prepend(node('p', '该岗位目前不在机会池中，以下为收藏时的记录；申请前请核对原公告。'));
      const label = node('label', '报名顺序 ');
      const input = document.createElement('input');
      input.type = 'number'; input.min = '1'; input.max = '10000'; input.step = '1';
      input.value = String(item.priority);
      input.setAttribute('aria-label', `${item.job.company} ${item.job.title} 报名顺序`);
      const button = node('button', '保存顺序'); button.type = 'button';
      button.addEventListener('click', async () => {
        if (!input.reportValidity() || !input.value) return;
        const token = session(); button.disabled = true;
        try {
          await api(`/future-radar/saved-jobs/${encodeURIComponent(item.job.id)}`, { method: 'PATCH', body: JSON.stringify({ priority: Number(input.value) }) });
          if (token !== session()) return;
          item.priority = Number(input.value); renderSaved(); toast('报名顺序已保存');
        } catch (_) { toast('报名顺序保存失败，请重试'); }
        finally { button.disabled = false; }
      });
      label.append(input, button); card.prepend(label); panel.append(card);
    });
  }
  function saveButton(job) {
    const button = node('button', saved.has(job.id) ? '★ 已收藏 · 取消收藏' : '☆ 收藏到报名清单', 'job-watch-button');
    button.type = 'button'; button.dataset.saveJob = job.id;
    button.setAttribute('aria-pressed', String(saved.has(job.id)));
    button.addEventListener('click', async () => {
      const token = session(); const exists = saved.has(job.id); button.disabled = true;
      const priority = Math.min(10000, Math.max(0, ...[...saved.values()].map(item => item.priority)) + 1);
      try {
        await api(`/future-radar/saved-jobs/${encodeURIComponent(job.id)}`, {
          method: exists ? 'DELETE' : 'PUT', ...(!exists ? { body: JSON.stringify({ priority }) } : {}),
        });
        if (token !== session()) return;
        if (exists) saved.delete(job.id); else saved.set(job.id, { job, priority });
        updateButtons(); renderSaved(); toast(exists ? '已取消收藏' : '已加入报名清单');
      } catch (_) { toast('收藏保存失败，请重试'); }
      finally { button.disabled = false; }
    });
    return button;
  }
  function showNotice(payload) {
    if (notice || !host.open || !payload.items.length) return;
    let reviewed = false;
    const dialog = document.createElement('dialog');
    dialog.className = 'radar-update-dialog';
    dialog.setAttribute('aria-label', '新增岗位提醒');
    dialog.append(node('h2', `发现 ${payload.items.length} 个新增岗位`), node('p', '请点击查看，核对岗位后确认关闭。未确认的提醒会保留。'));
    const body = node('div', '', 'radar-update-list'); body.hidden = true;
    const view = node('button', '查看新增岗位'); view.type = 'button';
    const close = node('button', '已查看，关闭提醒'); close.type = 'button'; close.hidden = true;
    const error = node('p', ''); error.setAttribute('role', 'alert');
    view.addEventListener('click', () => {
      body.replaceChildren(...payload.items.map(item => makeCard(item.job)));
      body.hidden = false; reviewed = true; view.hidden = true; close.hidden = false;
    });
    close.addEventListener('click', async () => {
      const token = session(); close.disabled = true;
      try {
        await api('/future-radar/notifications/ack', { method: 'POST', body: JSON.stringify({ through_event_id: payload.through_event_id }) });
        if (token !== session()) return;
        dialog.close(); dialog.remove(); notice = null; refresh();
      } catch (_) { error.textContent = '确认未保存，请重试；提醒仍然保留。'; }
      finally { close.disabled = false; }
    });
    dialog.addEventListener('cancel', event => { event.preventDefault(); if (reviewed) close.click(); });
    dialog.append(view, body, close, error); document.body.append(dialog); notice = dialog; dialog.showModal();
  }
  async function refresh() {
    const token = session();
    if (!token || !host.open || document.hidden || pending) return;
    if (owner !== token) { saved = new Map(); owner = token; }
    pending = true;
    try {
      const [bookmarks, notifications] = await Promise.allSettled([
        api('/future-radar/saved-jobs'), api('/future-radar/notifications'),
      ]);
      if (token !== session()) return;
      if (bookmarks.status === 'fulfilled') {
        saved = new Map(bookmarks.value.items.map(item => [item.job.id, item]));
        updateButtons();
        // Do not overwrite a priority value while the user is editing it.
        if (!panel.contains(document.activeElement)) renderSaved();
      }
      if (notifications.status === 'fulfilled') showNotice(notifications.value);
    } finally { pending = false; }
  }
  function start() { refresh(); if (!timer) timer = setInterval(refresh, 30000); }
  function stop() { clearInterval(timer); timer = null; }
  function reset() { stop(); owner = null; saved.clear(); notice?.close(); notice?.remove(); notice = null; panel.replaceChildren(); }
  host.addEventListener('close', stop);
  return { saveButton, renderSaved, start, reset };
}
